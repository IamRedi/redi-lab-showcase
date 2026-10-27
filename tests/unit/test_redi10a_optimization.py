"""REDI-10A deterministic offline foundation and dry lifecycle proofs."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import redi_lab.continual_optimization as co
from redi_lab.domain import StationId, WorkerId, WorkerProfile
from redi_lab.continual_optimization import (
    Approval,
    BudgetUse,
    DriftRule,
    DriftWindow,
    ExplorationBudget,
    MCWReference,
    OfflineFakeWorker,
    OfflineOptimizer,
    OptimizationError,
    OptimizationExperiment,
    OptimizationLedger,
    PromotionRule,
    RecipeRef,
    ShadowObservation,
    ShadowPair,
    State,
    classify_baseline,
    detect_drift,
    digest,
    eligible_downshift,
    onboard_worker,
    pareto_metrics,
)
from redi_lab.recipe_store import RecipeStore


MCW_MANIFEST = "c7d5606c340911fcbf5f2a233ac968de3768b1ef5eae13f108c9c23bc4bfa335"
MCW_RESULT = "04cde5b229eaff0641f8f62c317642a8e80e5cdd0d519161917f52141e67a5f0"
STATION = "station_mcw_implementation_v0"
TASK_CLASS = "mcw_python_component_v1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("REDI-10A must not use network/provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def experiment(*, max_experiments=2, min_pairs=1) -> OptimizationExperiment:
    baseline = RecipeRef("synthetic_recipe", 1, digest("baseline"), "SHADOW")
    candidate = RecipeRef("synthetic_recipe", 2, digest("candidate"), "CANDIDATE")
    return OptimizationExperiment(
        experiment_id="redi10a_synthetic_downshift_v1",
        version=1,
        baseline=baseline,
        candidate=candidate,
        treatment_delta={"worker_preference": {"from": "FRONTIER", "to": "MID"}},
        station_id=STATION,
        task_class=TASK_CLASS,
        budget=ExplorationBudget(max_experiments, 0, "0", 2, 60),
        evidence_requirements=("paired_verified_quality", "all_mandatory_gates", "replay"),
        regression_gates=("private_verification", "integration", "reviewer", "scope_security"),
        promotion_rule=PromotionRule(
            "redi10a_synthetic_rule", 1, min_pairs, True, True, True,
            "synthetic pre-result fixture, not formal production threshold", True,
        ),
        rollback_target=baseline,
        revalidation_policy_id="redi10a_targeted_revalidation_v1",
        valid_from="2026-09-25T00:00:00Z",
        valid_until="2026-10-25T00:00:00Z",
        mcw_evidence_ref={"experiment": "REDI-09 N8", "manifest_hash": MCW_MANIFEST, "result_hash": MCW_RESULT},
    )


def pair(manifest: OptimizationExperiment, *, task="task-1", candidate_quality=True, candidate_cost="4") -> ShadowPair:
    gates = {g: True for g in manifest.regression_gates}
    a = ShadowObservation(
        task_hash=digest(task), recipe=manifest.baseline,
        station_id=STATION, task_class=TASK_CLASS,
        worker_id="FRONTIER", worker_version="frozen-1",
        context_hash=digest("same-context"), verified=True, gates=gates,
        usage={"input": 100, "output": 20}, end_to_end_cost="10",
        latency_ms=1000, authoritative=True,
    )
    b = ShadowObservation(
        task_hash=digest(task), recipe=manifest.candidate,
        station_id=STATION, task_class=TASK_CLASS,
        worker_id="MID", worker_version="frozen-1",
        context_hash=digest("same-context"), verified=candidate_quality,
        gates=gates if candidate_quality else {**gates, "private_verification": False},
        usage={"input": 100, "output": 20}, end_to_end_cost=candidate_cost,
        latency_ms=800, failures=() if candidate_quality else ("PRIVATE_TEST_FAILURE",),
        authoritative=False,
    )
    return ShadowPair(a, b)


def optimizer(tmp_path: Path, manifest: OptimizationExperiment | None = None) -> OfflineOptimizer:
    return OfflineOptimizer(manifest or experiment(), OptimizationLedger(tmp_path / "optimization.jsonl"))


def window(*, quality="1", cost="10", model="v1", sample=20) -> DriftWindow:
    return DriftWindow(
        STATION, TASK_CLASS, "MID", sample, "0.5", model, "provider-v1",
        digest("context-v1"), digest("gates-v1"), quality, cost, "1000",
    )


def test_existing_recipe_store_has_no_implicit_stable_promotion(tmp_path):
    with RecipeStore(tmp_path / "empty.sqlite") as store:
        verdict = classify_baseline(store, "missing")
    assert verdict["classification"] == "STABLE_BASELINE_NOT_YET_ESTABLISHED"
    assert verdict["validated_versions"] == []
    assert verdict["approved_production_promotion"] is False
    fake_version = SimpleNamespace(
        recipe=SimpleNamespace(version=1), lifecycle_state=co.RecipeRung.VALIDATED,
        canonical_hash=digest("fake"), parent_version=None,
        rollback_target_version=None, confidence=SimpleNamespace(to_dict=lambda: {}),
    )
    fake_store = SimpleNamespace(
        list_versions=lambda _id: (fake_version,),
        current_selected_version=lambda _id: 1,
        events=lambda _id: [],
    )
    assert classify_baseline(fake_store, "x")["classification"] == "STABLE_BASELINE_NOT_YET_ESTABLISHED"


def test_manifest_is_versioned_frozen_and_one_variable(tmp_path):
    m = experiment()
    assert len(m.manifest_hash) == 64
    o = optimizer(tmp_path, m)
    with pytest.raises(OptimizationError, match="manifest drift"):
        OfflineOptimizer(replace(m, task_class="other"), o.ledger)
    with pytest.raises(OptimizationError, match="one material"):
        replace(m, treatment_delta={"worker": "MID", "gate": "weaker"})
    assert replace(
        m, treatment_delta={"worker": "MID", "context": "smaller"},
        multi_variable_justification="explicit factorial fixture",
    ).multi_variable_justification
    with pytest.raises(OptimizationError, match="synthetic"):
        replace(m, synthetic_only=False)
    with pytest.raises(OptimizationError, match="scope/security"):
        replace(m, regression_gates=("private_verification", "integration", "reviewer"))


def test_budget_exhaustion_stops_and_retains_evidence(tmp_path):
    o = optimizer(tmp_path, experiment(max_experiments=1))
    o.charge(BudgetUse(experiments=1, wall_seconds=20))
    before = o.ledger.replay()
    with pytest.raises(OptimizationError, match="budget exhausted"):
        o.charge(BudgetUse(experiments=1))
    after = o.ledger.replay()
    assert after["budget"] == before["budget"]
    assert o.ledger.events()[-1]["event_type"] == "BUDGET_EXHAUSTED"
    with pytest.raises(OptimizationError):
        o.charge(BudgetUse(provider_calls=1))
    with pytest.raises(OptimizationError, match="STOP"):
        o.enter_shadow()


def test_scoped_mcw_prior_only():
    m = experiment()
    e = MCWReference("REDI-09 N8", MCW_MANIFEST, MCW_RESULT, STATION, TASK_CLASS, "MID", "gpt-4.1-2025-04-14", 8, 8, "CANDIDATE_SUFFICIENT")
    result = eligible_downshift(manifest=m, evidence=e)
    assert result["candidate_eligible"] is True
    assert "no promotion" in result["warning"]
    with pytest.raises(OptimizationError, match="not applicable"):
        eligible_downshift(manifest=replace(m, station_id="unrelated_station"), evidence=e)
    with pytest.raises(OptimizationError, match="not applicable"):
        eligible_downshift(manifest=m, evidence=replace(e, state="NOT_SUFFICIENT_OBSERVED"))


def test_shadow_isolation_and_no_duplicate_retry(tmp_path):
    o = optimizer(tmp_path)
    o.enter_shadow()
    p = pair(o.manifest)
    o.record_pair(p)
    with pytest.raises(OptimizationError, match="duplicate"):
        o.record_pair(p)
    with pytest.raises(OptimizationError, match="output authority"):
        o.record_pair(replace(p, candidate=replace(p.candidate, authoritative=True)))
    assert o.ledger.replay()["active_recipe"] == co.asdict(o.manifest.baseline)


def test_frozen_fake_workers_run_same_input_without_candidate_authority(tmp_path):
    o = optimizer(tmp_path)
    o.enter_shadow()
    task = {"source": "def f(): return 1", "task_class": TASK_CLASS}
    task_hash = digest(task)
    def frozen(worker_id: str, cost: str) -> OfflineFakeWorker:
        return OfflineFakeWorker(worker_id, "v1", {task_hash: {
            "context_hash": digest("context"), "verified": True,
            "gates": {g: True for g in o.manifest.regression_gates},
            "usage": {"input": 20, "output": 4},
            "end_to_end_cost": cost, "latency_ms": 500,
        }})
    p = o.run_fake_shadow(task, frozen("FRONTIER", "10"), frozen("MID", "4"))
    assert p.baseline.task_hash == p.candidate.task_hash == task_hash
    assert p.baseline.authoritative and not p.candidate.authoritative
    assert o.evaluate() == State.ELIGIBLE_FOR_PROMOTION


def test_cheaper_same_quality_eligible_but_not_auto_promoted(tmp_path):
    o = optimizer(tmp_path)
    o.enter_shadow()
    p = pair(o.manifest)
    o.record_pair(p)
    assert o.evaluate() == State.ELIGIBLE_FOR_PROMOTION
    assert o.state == State.ELIGIBLE_FOR_PROMOTION
    assert o.ledger.replay()["active_recipe"] == co.asdict(o.manifest.baseline)
    with pytest.raises(OptimizationError, match="approval"):
        o._transition(State.PROMOTED, "unauthorized", {})
    metrics = pareto_metrics([p])
    assert metrics["successful_downshift_rate"] == "1/1"
    assert metrics["cost_delta"] == "-6"
    assert metrics["worker_selection_regret"] == "not_estimated_from_shadow_only"


def test_cheaper_regressed_candidate_rejected(tmp_path):
    o = optimizer(tmp_path)
    o.enter_shadow()
    o.record_pair(pair(o.manifest, candidate_quality=False))
    assert o.evaluate() == State.REJECTED
    assert o.ledger.replay()["active_recipe"] == co.asdict(o.manifest.baseline)
    with pytest.raises(OptimizationError, match="not eligible"):
        o.promote(Approval("human", "audit", digest(o.ledger.replay()["pairs"])))


def test_explicit_promotion_then_exact_immutable_rollback(tmp_path):
    o = optimizer(tmp_path)
    o.enter_shadow()
    o.record_pair(pair(o.manifest))
    o.evaluate()
    o.promote(Approval("human", "audit-1", digest(o.ledger.replay()["pairs"])))
    assert o.ledger.replay()["active_recipe"] == co.asdict(o.manifest.candidate)
    with pytest.raises(OptimizationError, match="exact immutable"):
        o.rollback(regression="private gate", approval=Approval("human", "audit-2", digest("wrong")))
    o.rollback(regression="private verification regression", approval=Approval("human", "audit-3", o.manifest.baseline.canonical_hash))
    replay = o.ledger.replay()
    assert replay["state"] == State.ROLLED_BACK
    assert replay["active_recipe"] == co.asdict(o.manifest.baseline)
    assert replay["manifest"]["candidate"]["canonical_hash"] == o.manifest.candidate.canonical_hash


def test_post_promotion_regression_triggers_exact_rollback(tmp_path):
    o = optimizer(tmp_path)
    o.enter_shadow()
    o.record_pair(pair(o.manifest))
    o.evaluate()
    o.promote(Approval("human", "audit-1", digest(o.ledger.replay()["pairs"])))
    regressed = replace(pair(o.manifest, candidate_quality=False).candidate, authoritative=True)
    assert o.post_promotion_check(
        regressed,
        rollback_approval=Approval("human", "preauthorized-rollback", o.manifest.baseline.canonical_hash),
    ) is True
    replay = o.ledger.replay()
    assert replay["state"] == State.ROLLED_BACK
    assert replay["active_recipe"] == co.asdict(o.manifest.baseline)
    assert replay["decisions"][-1]["evidence"]["regression_evidence_hash"] == digest(co.asdict(regressed))


def test_drift_requires_sustained_evidence_and_targets_scope(tmp_path):
    rule = DriftRule("descriptive_v0", 1, 10, 2, "0.1", "0.2", "0.2", "0.2")
    base = window()
    assert detect_drift(rule, base, [window(quality="0.8")])["state"] == "NO_DRIFT"
    o = optimizer(tmp_path)
    drift = o.observe_drift(rule, base, [window(quality="0.8"), window(quality="0.8")])
    assert drift["state"] == "TARGETED_REVALIDATION_REQUIRED"
    assert drift["signals"] == ["QUALITY_REGRESSION"]
    o.targeted_revalidation(drift, evidence_refs=[digest("targeted-replay")], decision="RETAIN")
    revalidation = o.ledger.replay()["drift"][-1]
    assert revalidation["affected_scope"] == {"station_id": STATION, "task_class": TASK_CLASS, "worker_id": "MID"}
    assert o.state == State.CANDIDATE


def test_new_worker_profile_uses_same_candidate_path():
    fake = WorkerProfile(
        id=WorkerId("worker_future_v1"), display_name="Offline future worker",
        version="1", station_eligibility=(StationId(STATION),),
        provider_opaque_reference="fake:future",
    )
    result = onboard_worker(
        fake, station_id=STATION, task_class=TASK_CLASS,
        historical_replay_hash=digest("replay"), shadow_evidence_hash=digest("shadow"),
        budget=experiment().budget,
    )
    assert result["stages"] == ["CANDIDATE_POOL", "HISTORICAL_REPLAY", "SHADOW", "BOUNDED_EXPLORATION", "ELIGIBILITY_DECISION"]
    assert result["production_authority"] is False
    with pytest.raises(OptimizationError, match="eligibility"):
        onboard_worker(fake, station_id="other", task_class=TASK_CLASS,
                       historical_replay_hash=digest("replay"), shadow_evidence_hash=digest("shadow"), budget=experiment().budget)


def test_no_silent_learning_and_replay_twice_under_config_drift(tmp_path, monkeypatch):
    o = optimizer(tmp_path)
    o.enter_shadow()
    o.record_pair(pair(o.manifest))
    before = o.ledger.replay()
    raw_before = o.ledger.path.read_bytes()
    historical_trace = {"worker_preference": "SMALL", "gate": "weakened"}
    historical_trace["worker_preference"] = "FRONTIER"
    monkeypatch.setattr(co, "POLICY_ID", "drifted-policy")
    one = o.ledger.replay()
    two = o.ledger.replay()
    assert one == two == before
    assert o.ledger.path.read_bytes() == raw_before
    assert one["replay_dependencies"] == {"provider_calls": 0, "network": 0, "current_registry": False, "current_config": False}
    lines = raw_before.decode("utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["manifest"]["task_class"] = "forged"
    o.ledger.path.write_text(co.canonical(tampered) + "\n" + "\n".join(lines[1:]) + "\n", encoding="utf-8")
    with pytest.raises(OptimizationError, match="hash chain"):
        o.ledger.replay()


def test_full_timeline_replay_twice_under_registry_and_policy_drift(tmp_path, monkeypatch):
    o = optimizer(tmp_path)
    o.charge(BudgetUse(experiments=1, wall_seconds=5))
    o.enter_shadow()
    o.record_pair(pair(o.manifest))
    o.evaluate()
    o.promote(Approval("human", "audit-promotion", digest(o.ledger.replay()["pairs"])))
    rule = DriftRule("descriptive_v0", 1, 10, 2, "0.1", "0.2", "0.2", "0.2")
    drift = o.observe_drift(rule, window(), [window(quality="0.8"), window(quality="0.8")])
    o.targeted_revalidation(drift, evidence_refs=[digest("targeted-evidence")], decision="ROLLBACK_REQUIRED")
    observation = replace(pair(o.manifest, candidate_quality=False).candidate, authoritative=True)
    o.post_promotion_check(
        observation,
        rollback_approval=Approval("human", "preauthorized-rollback", o.manifest.baseline.canonical_hash),
    )
    first = o.ledger.replay()
    monkeypatch.setattr(co, "POLICY_ID", "unrelated-current-policy")
    monkeypatch.setattr(co, "detect_drift", lambda *_a, **_kw: {"state": "forged"})
    second = o.ledger.replay()
    assert first == second
    assert first["state"] == State.ROLLED_BACK
    assert first["manifest"]["candidate"]["canonical_hash"] == o.manifest.candidate.canonical_hash
    assert len(first["pairs"]) == 1
    assert [d["event_type"] for d in first["drift"]] == [
        "DRIFT_OBSERVED", "REVALIDATION_DECIDED", "POST_PROMOTION_OBSERVATION",
    ]
    assert [d["to"] for d in first["decisions"]] == [
        State.SHADOW, State.ELIGIBLE_FOR_PROMOTION, State.PROMOTED, State.ROLLED_BACK,
    ]
    assert first["active_recipe"] == co.asdict(o.manifest.baseline)


def test_no_provider_network_and_no_recipe_store_mutation(tmp_path):
    recipe_path = tmp_path / "recipes.sqlite"
    with RecipeStore(recipe_path) as store:
        initial = classify_baseline(store, "synthetic_recipe")
    original = recipe_path.read_bytes()
    o = optimizer(tmp_path)
    o.enter_shadow()
    o.record_pair(pair(o.manifest))
    o.evaluate()
    assert recipe_path.read_bytes() == original
    assert initial["classification"] == "STABLE_BASELINE_NOT_YET_ESTABLISHED"
