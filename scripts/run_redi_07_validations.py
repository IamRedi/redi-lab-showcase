"""REDI-07 canonical 26-check validation pack (A-Z, fully offline)."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Callable

from redi_lab.cold_start import (
    COLD_START_UNKNOWN_PRODUCT,
    CandidateGenerationError,
    ColdStartPolicy,
    ExplorationBudget,
    canonical_cold_start_replay_view,
    controlled_product_spec,
    controlled_stations,
    derive_product_class,
    derive_product_signature,
    failure_action_table,
    generate_recipe_candidate,
    plan_cold_start_v0,
    replay_cold_start,
    run_controlled_cold_start,
)
from redi_lab.dag_scheduler import NodeRunState, product_dag_hash
from redi_lab.domain import (
    FactoryRun,
    FailureCategory,
    LifecycleEventType,
    Mode,
    RunStatus,
)
from redi_lab.persistence import EvidenceLedger, FactoryRunRepository, bootstrap_schema
from redi_lab.live_cold_start import (
    LIVE_MODEL,
    LIVE_WORKER_ID,
    PLANNED_LIVE_PROVIDER_CALLS,
    audit_live_cold_start,
    execute_live_cold_start,
    pre_execution_estimate,
)


def _count(db: Path, sql: str, params=()) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _plan(variant: str):
    product = controlled_product_spec(material_variant=variant)
    signature = derive_product_signature(product)
    product_class = derive_product_class(product, signature)
    product.product_class_id = product_class.id
    plan = plan_cold_start_v0(
        product=product,
        product_class=product_class,
        signature=signature,
        stations=controlled_stations(),
        policy=ColdStartPolicy(),
    )
    return signature, plan


def main() -> int:
    checks: list[tuple[str, Callable[[], None]]] = []
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="redi07a_validation_") as raw:
        root = Path(raw)
        verified = run_controlled_cold_start(
            output_root=root / "verified", known_recipe=None
        )
        replay = replay_cold_start(
            db_path=verified.db_path, factory_run_id=verified.factory_run_id
        )

        budget_policy = ColdStartPolicy(
            budget=ExplorationBudget(
                max_candidate_workers_per_node=1,
                max_attempts_per_worker=1,
                max_total_attempts_per_node=1,
                max_total_attempts_per_run=2,
            )
        )
        budget = run_controlled_cold_start(
            output_root=root / "budget", known_recipe=None, policy=budget_policy
        )
        budget_replay = replay_cold_start(
            db_path=budget.db_path, factory_run_id=budget.factory_run_id
        )
        unverified = run_controlled_cold_start(
            output_root=root / "unverified",
            known_recipe=None,
            force_unverified=True,
        )
        live_dry = execute_live_cold_start(
            live=False,
            output_root=root / "redi07b-live-dry",
        )
        live_audit = audit_live_cold_start(
            live_dry.db_path,
            factory_run_id=live_dry.factory_run_id,
            expected_live=False,
        )
        live_estimate = pre_execution_estimate()

        sig_a, plan_a = _plan("v1")
        sig_b, plan_b = _plan("v1")
        sig_changed, plan_changed = _plan("material-change")

        def check(label: str, condition: bool) -> None:
            if not condition:
                raise AssertionError(label)

        checks.extend(
            [
                ("A unknown product selects explicit cold-start mode", lambda: check("mode", verified.selection.mode == COLD_START_UNKNOWN_PRODUCT and replay["recipe_lookup_result"] == "none")),
                ("B same semantics/policy produce same DAG/hash", lambda: check("determinism", sig_a.sha256 == sig_b.sha256 and product_dag_hash(plan_a.dag) == product_dag_hash(plan_b.dag))),
                ("C material requirement change changes signature/DAG", lambda: check("material change", sig_a.sha256 != sig_changed.sha256 and product_dag_hash(plan_a.dag) != product_dag_hash(plan_changed.dag))),
                ("D Worker A deterministic failure classified", lambda: check("classified failure", replay["failures"][0]["category"] == FailureCategory.DETERMINISTIC_TEST_FAILURE.value)),
                ("E failed attempt remains after success", lambda: check("retained failure", bool(replay["failures"]) and replay["final_verification_state"])),
                ("F failure-specific escalation policy complete", lambda: check("routing", all(category.value in failure_action_table() for category in (FailureCategory.DETERMINISTIC_TEST_FAILURE, FailureCategory.CONTEXT_INSUFFICIENCY, FailureCategory.CAPABILITY_INSUFFICIENCY, FailureCategory.TOOL_FAILURE, FailureCategory.ENVIRONMENT_FAILURE, FailureCategory.SCOPE_VIOLATION, FailureCategory.INTEGRATION_CONFLICT, FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION)))),
                ("G Worker B succeeds on unchanged station/node", lambda: check("stable identity", len({item["node_id"] for item in replay["attempt_trajectory"] if item["node_id"] == "implement_normalize"}) == 1 and len({item["station_id"] for item in replay["attempt_trajectory"] if item["node_id"] == "implement_normalize"}) == 1)),
                ("H exploration budget enforced with typed failure", lambda: check("budget", not budget.verified and FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION.value in {item["category"] for item in budget_replay["failures"]})),
                ("I independent roots execute through parallel DAG", lambda: check("parallel roots", verified.execution.launch_order[:2] == ("implement_normalize", "implement_reorder") and verified.execution.final_states["implement_normalize"] == NodeRunState.COMPLETED)),
                ("J integration and independent reviewer required", lambda: check("review gates", {"integration_verification", "independent_final_reviewer"}.issubset({item["identity"] for item in replay["gates"] if item["status"] == "pass"}))),
                ("K unverified product creates no candidate", lambda: check("unverified guard", not unverified.verified and unverified.candidate is None)),
                ("L verified product creates exactly one candidate", lambda: check("one candidate", verified.candidate is not None and _count(verified.db_path, "SELECT COUNT(*) FROM evidence_entries WHERE payload_json LIKE '%RECIPE_CANDIDATE_CREATED%'") == 1)),
                ("M candidate references run/graph/evidence", lambda: check("provenance", verified.candidate is not None and verified.candidate.recipe.provenance["source_factory_run_id"] == verified.factory_run_id and bool(verified.candidate.recipe.provenance["relevant_failure_record_ids"]))),
                ("N candidate generation idempotent", lambda: _check_idempotent(verified)),
                ("O candidate stays NOT_PROMOTED", lambda: check("promotion guard", verified.candidate is not None and verified.candidate.recipe.metadata["promotion_status"] == "NOT_PROMOTED" and LifecycleEventType.RECIPE_PROMOTED.value not in replay["event_types"])),
                ("P replay x2 identical and read-only", lambda: _check_replay_read_only(verified)),
                ("Q current config drift cannot rewrite history", lambda: _check_config_drift(verified, replay)),
                ("R invalid/missing evidence blocks candidate", lambda: _check_missing_evidence(root / "missing.sqlite")),
                ("S zero provider/network calls; no experiment", lambda: check("offline", verified.provider_calls == 0 and verified.network_calls == 0 and _count(verified.db_path, "SELECT COUNT(*) FROM experiment_manifests") == 0)),
                ("T REDI-07B exact plan contains one live slot", lambda: check("one live slot", PLANNED_LIVE_PROVIDER_CALLS == 1 and live_estimate["planned_live_provider_calls"] == 1 and live_audit["would_be_live_calls"] == 1)),
                ("U REDI-07B weak A escalates to live-profile B on same node/station", lambda: _check_live_trajectory(live_audit)),
                ("V REDI-07B live context is selected/bounded/public", lambda: _check_live_context(live_audit)),
                ("W REDI-07B dry exact path verifies and creates one candidate", lambda: check("dry candidate", live_dry.verified and live_dry.candidate is not None and live_audit["checks"]["candidate_refs_resolve"])),
                ("X REDI-07B replay x2 is identical and read-only", lambda: check("live replay", live_audit["checks"]["replay_x2_identical"] and live_audit["checks"]["replay_read_only"])),
                ("Y REDI-07B candidate remains NOT_PROMOTED", lambda: check("live promotion guard", live_audit["replay"]["recipe_candidate"]["promotion_status"] == "NOT_PROMOTED" and not live_audit["replay"]["recipe_promoted_event"])),
                ("Z REDI-07B dry path uses accepted model with zero provider/network", lambda: check("model/offline", live_audit["live_attempt"]["worker_id"] == LIVE_WORKER_ID and live_audit["live_attempt"]["model"] == LIVE_MODEL and live_dry.provider_calls == 0 and live_dry.network_calls == 0 and live_audit["acceptance_valid"])),
            ]
        )

        for index, (name, fn) in enumerate(checks, 1):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - validation aggregation
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"  FAIL {chr(64 + index)} {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"  PASS {chr(64 + index)} {name}")

    print("=" * 64)
    print(f"TOTAL: PASS={len(checks) - len(failures)} FAIL={len(failures)} CHECKS={len(checks)}")
    if failures:
        for failure in failures:
            print("  - " + failure)
        return 1
    print("All REDI-07A/07B canonical validations passed (offline; no provider/network).")
    return 0


def _check_idempotent(outcome) -> None:
    before = _count(outcome.db_path, "SELECT COUNT(*) FROM evidence_entries")
    first = outcome.candidate
    second = generate_recipe_candidate(
        db_path=outcome.db_path, factory_run_id=outcome.factory_run_id
    )
    after = _count(outcome.db_path, "SELECT COUNT(*) FROM evidence_entries")
    if first is None or first.identity_hash != second.identity_hash or before != after:
        raise AssertionError("candidate identity/count changed")


def _check_replay_read_only(outcome) -> None:
    before = outcome.db_path.read_bytes()
    stat = outcome.db_path.stat()
    one = canonical_cold_start_replay_view(
        replay_cold_start(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    )
    two = canonical_cold_start_replay_view(
        replay_cold_start(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    )
    after_stat = outcome.db_path.stat()
    if one != two or before != outcome.db_path.read_bytes() or stat.st_mtime_ns != after_stat.st_mtime_ns:
        raise AssertionError("replay changed or wrote historical state")


def _check_config_drift(outcome, historical_view) -> None:
    import redi_lab.cold_start as cold_start_module

    originals = {
        "PLANNER_POLICY_ID": cold_start_module.PLANNER_POLICY_ID,
        "CONTEXT_STRATEGY_ID": cold_start_module.CONTEXT_STRATEGY_ID,
        "EXPLORATION_POLICY_ID": cold_start_module.EXPLORATION_POLICY_ID,
        "plan_cold_start_v0": cold_start_module.plan_cold_start_v0,
        "_controlled_worker_configuration": cold_start_module._controlled_worker_configuration,
    }
    def forbidden(*_args, **_kwargs):
        raise AssertionError("historical replay consulted current configuration")
    try:
        cold_start_module.PLANNER_POLICY_ID = "future_planner_v99"
        cold_start_module.CONTEXT_STRATEGY_ID = "future_context_v99"
        cold_start_module.EXPLORATION_POLICY_ID = "future_exploration_v99"
        cold_start_module.plan_cold_start_v0 = forbidden
        cold_start_module._controlled_worker_configuration = forbidden
        current = canonical_cold_start_replay_view(
            replay_cold_start(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
        )
        if current != canonical_cold_start_replay_view(historical_view):
            raise AssertionError("historical replay changed under current config drift")
    finally:
        for name, value in originals.items():
            setattr(cold_start_module, name, value)


def _check_missing_evidence(db: Path) -> None:
    conn = sqlite3.connect(db)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    repo = FactoryRunRepository(conn, ledger)
    run = FactoryRun(mode=Mode.EXPLORATION, final_status=RunStatus.RUNNING)
    repo.store(run)
    repo.close_run(str(run.id), final_status=RunStatus.COMPLETED, verified=True)
    conn.close()
    try:
        generate_recipe_candidate(db_path=db, factory_run_id=str(run.id))
    except CandidateGenerationError:
        return
    raise AssertionError("missing evidence unexpectedly created a candidate")


def _check_live_trajectory(audit: dict) -> None:
    attempts = [
        item for item in audit["replay"]["attempt_trajectory"]
        if item["node_id"] == "implement_normalize"
    ]
    if [item["worker_id"] for item in attempts] != [
        "worker_cold_cheap_weak_v0",
        LIVE_WORKER_ID,
    ]:
        raise AssertionError("unexpected REDI-07B worker trajectory")
    if attempts[0]["failure_category"] != FailureCategory.DETERMINISTIC_TEST_FAILURE.value:
        raise AssertionError("weak failure was not deterministic_test_failure")
    if attempts[0]["failure_action"] != "worker_escalation":
        raise AssertionError("weak failure did not cause explicit worker escalation")
    if len({item["node_id"] for item in attempts}) != 1 or len({item["station_id"] for item in attempts}) != 1:
        raise AssertionError("node/station identity changed during escalation")


def _check_live_context(audit: dict) -> None:
    context = audit["live_attempt"]["context"]
    required = {"strategy_id", "strategy_version", "source_list", "package_hash", "rendered_hash", "bytes", "estimated_tokens", "upstream_handoff_ids"}
    if not required.issubset(context) or context["strategy_id"] != "selected_v0":
        raise AssertionError("live context identity is incomplete")
    rendered = str(context).lower()
    if "hidden_tests" in rendered or "api_key" in rendered or "conversation" in rendered:
        raise AssertionError("live context contains forbidden material")


if __name__ == "__main__":
    raise SystemExit(main())
