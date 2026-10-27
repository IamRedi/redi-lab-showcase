"""REDI-10C1 qualification-evidence design tests — offline; zero provider/network."""

from __future__ import annotations

import socket
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from redi_lab.recipe_store import RecipeStore
from redi_lab.qualification_evidence import (
    BASELINE_MODEL,
    MANDATORY_GATES,
    V6_CANONICAL_HASH,
    build_evidence_manifest,
    candidate_frame_hash,
    capture_prior_operational_state,
    class_compatibility_proof,
    distinctness_proof,
    first_product_evidence,
    live_plan,
    paired_specs,
    post_acquisition_bundle,
    rehearse_restoration,
    select_products,
    selection_ranks,
    selection_seed,
)
from redi_lab.stable_baseline import (
    STABLE_BASELINE_CRITERIA_V0,
    STABLE_BASELINE_CRITERIA_V2,
    BootstrapPriorState,
    ProductHoldoutEvidence,
    QualificationApproval,
    QualificationLedger,
    QualificationManifest,
    RecipeRef,
    StableBaselineError,
    StableBaselineGovernor,
    State,
    digest,
    evaluate_criteria,
)


def evaluate_criteria_v1_bundle(bundle):
    """Evaluate a bundle under a v1-criteria manifest (offline decision core)."""
    manifest = QualificationManifest(
        qualification_id="current_state_probe",
        version=1,
        recipe=RecipeRef("recipe_cold_39553c39ceb593e2bc24775be1c420e2", 6, V6_CANONICAL_HASH, "SHADOW"),
        criteria=STABLE_BASELINE_CRITERIA_V2,
        rollback_target=RecipeRef("recipe_cold_39553c39ceb593e2bc24775be1c420e2", 1, digest("pin"), "SHADOW"),
        station_id=STATION,
        task_class=TASK_CLASS,
        valid_from="2026-09-25T00:00:00Z",
        valid_until="2026-12-24T00:00:00Z",
    )
    return evaluate_criteria(manifest, bundle)

STORE = "build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite"
STATION = "station_mcw_implementation_v0"
TASK_CLASS = "mcw_python_component_v1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("REDI-10C1 must not use network/provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture()
def captured() -> dict:
    with RecipeStore(STORE) as store:
        before = Path(STORE).read_bytes()
        cap = capture_prior_operational_state(store)
        unchanged = Path(STORE).read_bytes() == before
    assert unchanged
    return cap


# ---------------------------------------------------------------------------
# 1. rollback state capture
# ---------------------------------------------------------------------------


def test_prior_state_capture_deterministic_and_complete(captured) -> None:
    with RecipeStore(STORE) as store:
        again = capture_prior_operational_state(store)
    assert again["captured_state_hash"] == captured["captured_state_hash"]
    assert captured["rehearsal"]["all_passed"] is True
    assert captured["rehearsal"]["provider_calls"] == 0
    assert captured["rehearsal"]["store_mutations"] == 0
    assert captured["definition"]["fallback_worker_id"] == "worker_cold_openai_live_fallback_v0"
    assert captured["definition"]["fallback_model"] == BASELINE_MODEL
    assert captured["definition"]["reviewer_execution"].startswith("local deterministic")
    assert len(captured["captured_state_hash"]) == 64
    assert len(captured["restoration_procedure_hash"]) == 64
    assert len(captured["restoration_procedure"]) == 3


def test_restoration_rehearsal_is_read_only_and_twice_identical(captured) -> None:
    with RecipeStore(STORE) as store:
        raw_before = Path(STORE).read_bytes()
        r1 = rehearse_restoration(store, captured["definition"])
        r2 = rehearse_restoration(store, captured["definition"])
        assert Path(STORE).read_bytes() == raw_before
    assert r1 == r2 and r1["all_passed"]


# ---------------------------------------------------------------------------
# 2-3. candidate frame + deterministic neutral selection
# ---------------------------------------------------------------------------


def test_frame_larger_than_selection_and_deterministic() -> None:
    from redi_lab.qualification_evidence import _candidate_frame

    assert len(_candidate_frame()) == 4
    assert candidate_frame_hash() == candidate_frame_hash()
    first, second = select_products()
    for _ in range(4):
        assert select_products() == (first, second)
    ranks = selection_ranks()
    ordered = sorted(ranks, key=lambda k: (ranks[k], k))
    assert tuple(ordered[:2]) == (first.key, second.key)


def test_selection_seed_excludes_result_material() -> None:
    seed = selection_seed()
    material = "96da7671a285e36710e4a119c428fed619cae9597c0e3dda4d6dc60c04e78ffd" + V6_CANONICAL_HASH + "REDI-10C1-evidence-v1"
    import hashlib

    assert seed == hashlib.sha256(material.encode()).hexdigest()
    lowered = material.lower()
    for banned in ("gpt-", "failure", "verified", "difficulty"):
        assert banned not in lowered


def test_selected_products_distinct_and_class_compatible() -> None:
    first, second = select_products()
    proof = distinctness_proof()
    assert proof["pairwise_distinct"] and proof["forbidden_key_overlaps"] == []
    assert proof["distinct_from_source_redi07_product"]
    assert proof["distinct_from_depot_holdout"]
    assert proof["distinct_from_existing_markers"]
    for candidate in (first, second):
        compat = class_compatibility_proof(candidate)
        assert compat["compatible"], compat


# ---------------------------------------------------------------------------
# 4-8. paired specs + manifest freeze + approval slots only
# ---------------------------------------------------------------------------


def test_paired_specs_hold_constants_and_one_call() -> None:
    specs = paired_specs()
    assert len(specs) == 2
    for spec in specs:
        d = spec.to_dict()
        assert d["baseline_arm"]["provider_calls"] == 1
        assert d["v6_arm"]["provider_calls"] == 1
        assert d["shared"]["automatic_retry"] is False
        assert d["shared"]["one_call_per_arm"] is True
        assert d["shared"]["worker_fallback"] == "forbidden"
        assert d["shared"]["reviewer_execution"].startswith("local deterministic")
        assert d["v6_arm"]["worker_profile_version"] == 3
        assert d["v6_arm"]["reasoning_effort"] == "low"
        assert d["baseline_arm"]["worker_id"] == "worker_cold_openai_live_fallback_v0"


def test_manifest_frozen_bindings_and_slots(captured) -> None:
    m = build_evidence_manifest(captured)
    m2 = build_evidence_manifest(captured)
    assert m.manifest_hash == m2.manifest_hash
    assert m.criteria_v2_hash == STABLE_BASELINE_CRITERIA_V2.criteria_hash
    assert m.recipe == {
        "recipe_id": "recipe_cold_39553c39ceb593e2bc24775be1c420e2",
        "version": 6,
        "canonical_hash": V6_CANONICAL_HASH,
        "lifecycle": "SHADOW",
    }
    assert m.first_product_evidence["verified"] is True
    assert m.first_product_evidence["product_id"] == "redi08_depot_replenishment_holdout"
    assert m.rollback_captured_state_hash == captured["captured_state_hash"]
    assert m.no_retry is True and m.replay_required is True
    assert m.approval_slots == {"validated": None, "stable": None}
    assert m.validity_window == {"max_days": 90}
    # approval slots only: the manifest never contains an approval object
    assert "approved_by" not in __import__("json").dumps(asdict(m))


# ---------------------------------------------------------------------------
# 9. live plan
# ---------------------------------------------------------------------------


def test_live_plan_counts_and_bounds() -> None:
    plan = live_plan()
    assert plan["provider_calls_total"] == 4
    assert plan["models_involved"] == ["gpt-4o-mini-2024-07-18", "gpt-5.6-terra"]
    assert plan["expected_cost_usd"] == "0.021708800"
    assert plan["hard_cost_bound_usd"] == "0.060416000"
    assert "chosen later by the owner" in plan["owner_ceiling"]


# ---------------------------------------------------------------------------
# 10. dry rehearsal with fake providers (both arms, both products)
# ---------------------------------------------------------------------------


def _fake_arm_result(product_key: str, arm: str, *, verified: bool) -> dict:
    gates = {g: verified for g in MANDATORY_GATES}
    usage = {"input": 1500, "output": 400} if arm == "v6_arm" else {"input": 1200, "output": 300}
    cost = "0.010000" if arm == "v6_arm" else "0.000400"
    return {
        "product_key": product_key,
        "arm": arm,
        "verified": verified,
        "gates": gates,
        "usage": usage,
        "end_to_end_cost": cost,
        "provider_calls": 1,
    }


def _product_evidence(product_key: str, *, v6_verified: bool, reconciled=True) -> ProductHoldoutEvidence:
    baseline = _fake_arm_result(product_key, "baseline_arm", verified=True)
    v6 = _fake_arm_result(product_key, "v6_arm", verified=v6_verified)
    return ProductHoldoutEvidence(
        product_id=product_key,
        transfer_evidence_hash=digest({"baseline": baseline, "v6": v6}),
        verified=v6["verified"],
        gates=v6["gates"],
        replay_x2_identical=True,
        quality_non_regression=v6["verified"],
        provider_calls=baseline["provider_calls"] + v6["provider_calls"],
        store_metadata_reconciled=reconciled,
    )


def _bootstrap_governor(tmp_path: Path, captured: dict) -> tuple:
    manifest = QualificationManifest(
        qualification_id="redi10c1_dry_qualification_v1",
        version=1,
        recipe=RecipeRef("recipe_cold_39553c39ceb593e2bc24775be1c420e2", 6, V6_CANONICAL_HASH, "SHADOW"),
        criteria=STABLE_BASELINE_CRITERIA_V2,
        rollback_target=RecipeRef("recipe_cold_39553c39ceb593e2bc24775be1c420e2", 1, digest("prior-state-pin"), "SHADOW"),
        station_id=STATION,
        task_class=TASK_CLASS,
        valid_from="2026-09-25T00:00:00Z",
        valid_until="2026-12-24T00:00:00Z",
        first_baseline=True,
    )
    bootstrap = BootstrapPriorState(
        state_id="no_promoted_recipe_fallback_v0",
        captured_state_hash=captured["captured_state_hash"],
        restoration_procedure_hash=captured["restoration_procedure_hash"],
        restoration_replay_x2_identical=True,
        fallback_description="no promoted recipe; REDI-07B fallback operational path",
        owner_approved_by="owner",
        owner_audit_reference="audit-bootstrap-001",
    )
    governor = StableBaselineGovernor(manifest, QualificationLedger(tmp_path / "q.jsonl"), bootstrap=bootstrap)
    return governor, manifest


def test_dry_rehearsal_full_path_eligible(tmp_path, captured) -> None:
    governor, _ = _bootstrap_governor(tmp_path, captured)
    # current-state bundle honestly fails completeness (depot unreconciled)
    current = evaluate_criteria_v1_bundle(
        [first_product_evidence()] + [
            _product_evidence(key, v6_verified=True) for key in ("stationery_supply", "pharmacy_supply")
        ]
    )
    assert "EVIDENCE_COMPLETENESS" in current["failed_criteria"]
    # post-acquisition bundle (depot reconciled, both new products verified)
    bundle = post_acquisition_bundle({"stationery_supply": True, "pharmacy_supply": True})
    result = governor.evaluate(bundle)
    assert result["criteria_met"] is True, result["failed_criteria"]
    assert result["checks"]["BOOTSTRAP_ROLLBACK_PATH"]["met"] is True
    assert governor.state is State.ELIGIBLE_FOR_VALIDATION  # never auto-promotion
    with pytest.raises(StableBaselineError, match="approval"):
        governor.approve_validated(
            QualificationApproval("anyone", "no-audit", digest("x"), "VALIDATED")
        )
    one, two = governor.ledger.replay(), governor.ledger.replay()
    assert one == two
    assert one["replay_dependencies"]["provider_calls"] == 0


def test_one_failed_product_blocks_qualification(tmp_path, captured) -> None:
    governor, _ = _bootstrap_governor(tmp_path, captured)
    bundle = post_acquisition_bundle({"stationery_supply": True, "pharmacy_supply": False})
    result = governor.evaluate(bundle)
    assert result["criteria_met"] is False
    assert "VERIFIED_TRANSFERS" in result["failed_criteria"]
    assert "QUALITY_NON_REGRESSION" in result["failed_criteria"]
    assert governor.state is State.SHADOW
    blocked = governor.ledger.replay()["blocked_evaluations"]
    assert blocked[-1]["no_promotion"] is True


def test_manifest_rollback_hash_feeds_bootstrap_path(tmp_path, captured) -> None:
    m = build_evidence_manifest(captured)
    assert m.rollback_captured_state_hash == captured["captured_state_hash"]
    # the frozen plan's captured hash slots directly into the bootstrap governor
    governor, _ = _bootstrap_governor(tmp_path, captured)
    assert governor.bootstrap.captured_state_hash == m.rollback_captured_state_hash
