"""REDI-07B exact live-runner path tests (all network-free)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import redi_lab.live_cold_start as live_module
from redi_lab.cold_start import COLD_START_UNKNOWN_PRODUCT, replay_cold_start
from redi_lab.domain import FailureCategory, LifecycleEventType
from redi_lab.live_cold_start import (
    ACTIVE_LIVE_CANDIDATE,
    DECLARED_LIVE_CANDIDATES,
    HISTORICAL_LIVE_CANDIDATE,
    LIVE_ACCEPTANCE_POLICY_ID,
    LIVE_ACCEPTANCE_POLICY_VERSION,
    LIVE_MODEL,
    LIVE_NODE_ID,
    LIVE_WORKER_ID,
    NEXT_LIVE_CANDIDATE,
    PLANNED_LIVE_PROVIDER_CALLS,
    LiveAcceptanceConfiguration,
    LiveCallGuard,
    audit_live_cold_start,
    build_live_configuration,
    execute_live_cold_start,
    pre_execution_estimate,
    validate_live_configuration,
)


@pytest.fixture(scope="module")
def dry_run(tmp_path_factory):
    return execute_live_cold_start(
        live=False,
        output_root=tmp_path_factory.mktemp("redi07b_dry") / "run",
    )


@pytest.fixture(scope="module")
def dry_audit(dry_run):
    return audit_live_cold_start(
        dry_run.db_path,
        factory_run_id=dry_run.factory_run_id,
        expected_live=False,
    )


def test_exact_provider_policy_is_one():
    estimate = pre_execution_estimate()
    assert PLANNED_LIVE_PROVIDER_CALLS == 1
    assert estimate["planned_live_provider_calls"] == 1
    assert estimate["live_node_id"] == LIVE_NODE_ID
    assert estimate["expected_upper_bound_tokens"] == (
        estimate["estimated_input_tokens"] + estimate["max_output_tokens"]
    )


def test_public_normalize_contract_is_semantically_complete_and_representative():
    product = live_module.controlled_product_spec()
    requirement = next(
        item.text for item in product.requirements
        if item.text.startswith("Normalize external SKU labels")
    )
    for phrase in (
        "require a string",
        "trim surrounding whitespace",
        "collapse each run",
        "remove leading and trailing hyphens",
        "normalize to empty",
    ):
        assert phrase in requirement

    public_test = (
        live_module.CONTROLLED_FIXTURE_ROOT / "tests" / "test_normalize_public.py"
    ).read_text(encoding="utf-8")
    assert 'normalize_sku(" --north__ 42-- ") == "NORTH-42"' in public_test
    assert "pytest.raises(TypeError)" in public_test
    assert "pytest.raises(ValueError)" in public_test


def test_dry_path_selects_unknown_product_and_planner_dag(dry_run):
    assert dry_run.selection.mode == COLD_START_UNKNOWN_PRODUCT
    assert dry_run.selection.recipe_lookup_result == "none"
    assert str(dry_run.plan.dag.id).startswith("run_cold_v0_")
    assert dry_run.execution.graph_hash


def test_weak_a_failure_then_same_node_station_live_b(dry_audit):
    attempts = [
        item for item in dry_audit["replay"]["attempt_trajectory"]
        if item["node_id"] == LIVE_NODE_ID
    ]
    assert [item["worker_id"] for item in attempts] == [
        "worker_cold_cheap_weak_v0",
        LIVE_WORKER_ID,
    ]
    assert attempts[0]["failure_category"] == FailureCategory.DETERMINISTIC_TEST_FAILURE.value
    assert attempts[0]["failure_action"] == "worker_escalation"
    assert len({item["station_id"] for item in attempts}) == 1


def test_dry_path_has_one_would_be_slot_and_zero_provider_network(dry_run, dry_audit):
    assert dry_audit["would_be_live_calls"] == 1
    assert dry_audit["actual_provider_calls"] == 0
    assert dry_run.provider_calls == 0
    assert dry_run.network_calls == 0
    assert dry_run.worker_call_counts[LIVE_WORKER_ID] == 1


def test_live_context_is_bounded_public_and_complete(dry_audit):
    context = dry_audit["live_attempt"]["context"]
    assert context["strategy_id"] == "selected_v0"
    assert context["strategy_version"] >= 1
    assert context["bytes"] > 0
    assert context["estimated_tokens"] > 0
    assert len(context["package_hash"]) == 64
    assert len(context["rendered_hash"]) == 64
    assert context["source_list"] == [
        "inventory_math/normalize.py",
        "tests/test_normalize_public.py",
    ]
    rendered = str(context).lower()
    assert "hidden_tests" not in rendered
    assert "api_key" not in rendered
    assert "conversation" not in rendered


def test_dry_path_strict_gates_verified_and_candidate_not_promoted(dry_run, dry_audit):
    assert dry_run.verified is True
    assert dry_audit["acceptance_valid"] is True
    assert dry_audit["checks"]["strict_gates"] is True
    candidate = dry_audit["replay"]["recipe_candidate"]
    assert candidate is not None
    assert candidate["promotion_status"] == "NOT_PROMOTED"
    assert LifecycleEventType.RECIPE_PROMOTED.value not in dry_audit["replay"]["event_types"]


def test_dry_audit_replay_twice_is_identical_read_only_and_refs_resolve(dry_audit):
    assert dry_audit["checks"]["replay_x2_identical"] is True
    assert dry_audit["checks"]["replay_read_only"] is True
    assert dry_audit["checks"]["candidate_refs_resolve"] is True
    assert dry_audit["checks"]["no_orphan_factory_evidence"] is True


def test_configuration_rejects_same_worker_retry():
    policy = live_module._policy()
    station = live_module.controlled_stations()[0]
    config = build_live_configuration(live=False, station=station, guard=LiveCallGuard())
    bad_policy = replace(
        policy,
        budget=replace(policy.budget, max_attempts_per_worker=2),
    )
    with pytest.raises(RuntimeError, match="same-worker retry"):
        validate_live_configuration(config, bad_policy)


def test_configuration_rejects_live_reviewer_and_second_live_node():
    policy = live_module._policy()
    station = live_module.controlled_stations()[0]
    config = build_live_configuration(live=False, station=station, guard=LiveCallGuard())
    with pytest.raises(RuntimeError, match="live planner/reviewer"):
        validate_live_configuration(replace(config, live_reviewer=True), policy)
    with pytest.raises(RuntimeError, match="exactly one live node"):
        validate_live_configuration(replace(config, live_node_ids=(LIVE_NODE_ID, "implement_reorder")), policy)


def test_live_worker_profile_binds_accepted_model():
    station = live_module.controlled_stations()[0]
    config = build_live_configuration(live=False, station=station, guard=LiveCallGuard())
    profile = config.normalize_pool.entries[1].profile
    assert str(profile.id) == LIVE_WORKER_ID
    assert profile.provider_opaque_reference == f"openai:{LIVE_MODEL}"


def test_post_failure_candidate_is_predeclared_and_versioned():
    assert ACTIVE_LIVE_CANDIDATE is NEXT_LIVE_CANDIDATE
    assert LIVE_WORKER_ID != HISTORICAL_LIVE_CANDIDATE.worker_id
    assert LIVE_MODEL != HISTORICAL_LIVE_CANDIDATE.model
    assert LIVE_ACCEPTANCE_POLICY_ID == NEXT_LIVE_CANDIDATE.policy_id
    assert LIVE_ACCEPTANCE_POLICY_VERSION == NEXT_LIVE_CANDIDATE.policy_version
    assert (
        LIVE_ACCEPTANCE_POLICY_ID,
        LIVE_ACCEPTANCE_POLICY_VERSION,
    ) in DECLARED_LIVE_CANDIDATES

    station = live_module.controlled_stations()[0]
    config = build_live_configuration(
        live=False,
        station=station,
        guard=LiveCallGuard(),
    )
    candidate = config.normalize_pool.entries[1].profile
    assert str(candidate.id) == NEXT_LIVE_CANDIDATE.worker_id
    assert candidate.provider_opaque_reference == f"openai:{NEXT_LIVE_CANDIDATE.model}"
    assert candidate.metadata["candidate_policy_id"] == NEXT_LIVE_CANDIDATE.policy_id
    assert candidate.metadata["candidate_policy_version"] == NEXT_LIVE_CANDIDATE.policy_version
    assert config.live_node_ids == (LIVE_NODE_ID,)


def test_guard_refuses_second_call():
    guard = LiveCallGuard()
    guard.claim()
    with pytest.raises(RuntimeError, match="budget exhausted"):
        guard.claim()
    assert guard.claimed == 1


def test_live_failure_is_terminal_persisted_and_candidate_free(tmp_path: Path, monkeypatch):
    original = live_module.build_live_configuration

    class FailingOneCallRunner:
        def __init__(self, guard):
            self.guard = guard

        def run_node(self, **_kwargs):
            self.guard.claim()
            self.guard.observe("provider_call_started", 10)
            self.guard.observe("provider_call_finished", 20)
            raise RuntimeError("controlled public provider failure")

    def failing_config(*, live, station, guard):
        config = original(live=False, station=station, guard=guard)
        runners = dict(config.runners)
        runners[LIVE_WORKER_ID] = FailingOneCallRunner(guard)
        return LiveAcceptanceConfiguration(
            normalize_pool=config.normalize_pool,
            reorder_pool=config.reorder_pool,
            runners=runners,
            live_worker_ids=config.live_worker_ids,
            live_node_ids=config.live_node_ids,
        )

    monkeypatch.setattr(live_module, "build_live_configuration", failing_config)
    outcome = execute_live_cold_start(live=True, output_root=tmp_path / "failed-live")
    audit = audit_live_cold_start(
        outcome.db_path,
        factory_run_id=outcome.factory_run_id,
        expected_live=True,
    )
    replay = replay_cold_start(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert outcome.verified is False
    assert outcome.candidate is None
    assert outcome.provider_calls == 1
    assert audit["valid_failed_acceptance"] is True
    assert audit["acceptance_valid"] is True
    assert replay["product_verified_event"] is False
    assert replay["recipe_candidate"] is None
    live_failures = [item for item in replay["failures"] if item["worker_id"] == LIVE_WORKER_ID]
    assert len(live_failures) == 1
    assert live_failures[0]["retry_escalation_decision"] == "stop_after_single_live_failure"


def test_default_runner_source_requires_explicit_live_opt_in():
    source = (Path(__file__).resolve().parents[2] / "scripts" / "run_redi_07_live_cold_start.py").read_text(encoding="utf-8")
    assert 'args.live' in source
    assert 'os.environ.get("REDI_LIVE_PROVIDER") != "1"' in source
    assert 'os.environ.get("REDI_OPENAI_API_KEY")' in source
