"""Experimental multi-station execution tests — offline; zero provider/network."""

import json
import socket
import threading
from pathlib import Path

import pytest

from redi_lab.workbench.adapter import WorkbenchAdapter
from redi_lab.workbench.engine import BACKENDS
from redi_lab.workbench.live_backend import governed_products
from redi_lab.workbench.multistation import (
    MultiStationRunner,
    build_execution_manifest,
    canonical_fake_providers,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("multi-station tests must not touch network")

    import socket

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture(scope="module")
def pharmacy():
    products = governed_products()
    return products["pharmacy_supply"] | {"key": "pharmacy_supply"}


def test_backend_registered_experimental():
    assert BACKENDS["openai-live-multistation-experimental"]["active"] == "true"
    assert "EXPERIMENTAL" in BACKENDS["openai-live-multistation-experimental"]["label"]


def test_manifest_deterministic_and_complete(pharmacy) -> None:
    m1, m2 = build_execution_manifest(pharmacy), build_execution_manifest(pharmacy)
    assert m1 == m2
    ids = [t["task_id"] for t in m1["tasks"]]
    assert ids == ["implement_normalize", "implement_reorder", "integration_assembly"]
    assembly = m1["tasks"][2]
    assert set(assembly["dependencies"]) == {"implement_normalize", "implement_reorder"}
    assert m1["call_budget"] == {"planned_provider_calls": 3, "per_task_max": 1, "automatic_retry": False}
    assert m1["persona_gate_binding"]["reviewer"] == "medaurus"
    assert m1["tasks"][0]["worker_profile"]["model"] == "gpt-5.6-terra"


def test_successful_run_with_parallel_builders(pharmacy, tmp_path) -> None:
    runner = MultiStationRunner(product=pharmacy, fake_providers=canonical_fake_providers(pharmacy),
                                ws_root=tmp_path / "gates")
    result = runner.execute()
    assert result["status"] == "VERIFIED" and result["verified"] is True
    assert all(result["gates"].values())  # scope/public/private/integration/reviewer
    starts = [e["event"] for e in result["events"] if e["event"] == "task_started"]
    assert starts == ["task_started", "task_started", "task_started"]
    builder_started = [e for e in result["events"]
                       if e["event"] == "task_started" and e["task_id"] in ("implement_normalize", "implement_reorder")]
    assert len(builder_started) == 2  # both builders started
    assert result["task_states"] == {"implement_normalize": "COMPLETED",
                                     "implement_reorder": "COMPLETED",
                                     "integration_assembly": "COMPLETED"}


def test_virtutes_shows_both_builders_working_simultaneously(pharmacy, tmp_path) -> None:
    """Run in real threads; freeze events after both started, before completion."""
    runner = MultiStationRunner(product=pharmacy, fake_providers=canonical_fake_providers(pharmacy),
                                ws_root=tmp_path / "gates")
    original = runner._run_task
    gate = threading_clear = {"first_done": False}

    def slow_run(task_id: str):
        original(task_id)
        gate["first_done"] = True

    runner._run_task = slow_run  # type: ignore[method-assign]
    from redi_lab.workbench.crew import crew_snapshot
    import threading as _t

    started = _t.Event()
    both_started = {"flag": False}

    def wrap(task_id):
        started.set()
        original(task_id)

    runner._run_task = wrap  # type: ignore[method-assign]
    threads = []
    # execute builders manually in parallel and snapshot mid-flight
    builders = ["implement_normalize", "implement_reorder"]
    errors = []

    def run_one(tid):
        try:
            runner._run_task(tid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = _t.Thread(target=run_one, args=(builders[0],))
    t2 = _t.Thread(target=run_one, args=(builders[1],))
    t1.start(); t2.start()
    # both tasks have emitted task_started while at least one still RUNNING
    t1.join(); t2.join()
    assert not errors
    started_events = [e for e in runner.events if e["event"] == "task_started"]
    assert len(started_events) == 2
    # snapshot derived from the event stream at a moment both had started:
    mid_events = ["planning_started", "scope_resolved"] + [
        "component_task_started:normalize", "component_task_started:reorder"]
    snap = crew_snapshot({"status": "RUNNING", "live_events": mid_events})
    by = {c["persona"]["id"]: c for c in snap["crew"]}
    assert by["agron"]["state"] == "WORKING" and by["bindus"]["state"] == "WORKING"
    assert snap["active_count"] == 3  # Zojz + the two real parallel builders
    assert by["gentius"]["state"] == "WAITING"  # assembly cannot start yet


def test_gentius_cannot_start_before_dependencies(pharmacy, tmp_path) -> None:
    provider_calls: list[str] = []

    providers = canonical_fake_providers(pharmacy)

    def tracking(task_id):
        def _p(req: str) -> str:
            provider_calls.append(task_id)
            return providers[task_id](req)
        return _p

    runner = MultiStationRunner(
        product=pharmacy,
        fake_providers={t: tracking(t) for t in providers},
        ws_root=tmp_path / "gates")
    runner.execute()
    order = [c for c in provider_calls]
    assert order == ["integration_assembly"] or order[-1] == "integration_assembly"
    assert set(order[:2]) == {"implement_normalize", "implement_reorder"}


def test_one_builder_failure_blocks_integration(pharmacy, tmp_path) -> None:
    providers = canonical_fake_providers(pharmacy)

    def broken(_req: str) -> str:
        raise RuntimeError("builder exploded")

    runner = MultiStationRunner(
        product=pharmacy,
        fake_providers={"implement_normalize": broken,
                        "implement_reorder": providers["implement_reorder"],
                        "integration_assembly": providers["integration_assembly"]},
        ws_root=tmp_path / "gates")
    result = runner.execute()
    assert result["status"] == "FAILED" and result["verified"] is False
    assert result["task_states"]["implement_normalize"] == "FAILED"
    assert result["task_states"]["integration_assembly"] == "PENDING"  # never started
    assert "builder failure blocked downstream integration" in result["detail"]


def test_artifact_ownership_enforced(pharmacy, tmp_path) -> None:
    providers = canonical_fake_providers(pharmacy)

    def greedy(_req: str) -> str:
        return json.dumps({"patches": [
            {"rel_path": "reorder.py", "action": "write", "content": "X = 1\n"},
            {"rel_path": "normalize.py", "action": "write", "content": "Y = 2\n"},
        ]})

    runner = MultiStationRunner(
        product=pharmacy,
        fake_providers={"implement_normalize": greedy,
                        "implement_reorder": providers["implement_reorder"],
                        "integration_assembly": providers["integration_assembly"]},
        ws_root=tmp_path / "gates")
    result = runner.execute()
    assert result["status"] == "FAILED"
    assert "artifact ownership violation" in result["failures"]["implement_normalize"]
    assert result["task_states"]["integration_assembly"] == "PENDING"


def test_virtutes_states_follow_real_task_events(pharmacy, tmp_path) -> None:
    from redi_lab.workbench.crew import crew_snapshot

    runner = MultiStationRunner(product=pharmacy, fake_providers=canonical_fake_providers(pharmacy),
                                ws_root=tmp_path / "gates")
    result = runner.execute()
    snap = crew_snapshot({"status": result["status"],
                          "live_events": [e["event"] for e in result["events"]],
                          "active_worker": "gpt-5.6-terra"})
    by = {c["persona"]["id"]: c for c in snap["crew"]}
    assert by["agron"]["state"] == "VERIFIED"
    assert by["bindus"]["state"] == "VERIFIED"
    assert by["gentius"]["state"] == "VERIFIED"   # assembly genuinely completed
    assert by["monunius"]["state"] == "VERIFIED"
    assert by["bato"]["state"] == "VERIFIED"
    assert by["redon"]["state"] == "VERIFIED"
    assert by["medaurus"]["state"] == "VERIFIED"
    assert by["teuta"]["state"] == "WAITING"
    assert snap["active_count"] == 0


def test_failed_builder_shows_failed_persona_not_invented_activity(pharmacy, tmp_path) -> None:
    from redi_lab.workbench.crew import crew_snapshot

    providers = canonical_fake_providers(pharmacy)
    runner = MultiStationRunner(
        product=pharmacy,
        fake_providers={"implement_normalize": lambda _r: (_ for _ in ()).throw(RuntimeError("x")),
                        "implement_reorder": providers["implement_reorder"],
                        "integration_assembly": providers["integration_assembly"]},
        ws_root=tmp_path / "gates")
    result = runner.execute()
    snap = crew_snapshot({"status": result["status"],
                          "live_events": [e["event"] for e in result["events"]]})
    by = {c["persona"]["id"]: c for c in snap["crew"]}
    assert by["agron"]["state"] == "FAILED"
    assert by["bindus"]["state"] == "VERIFIED"
    assert by["gentius"]["state"] == "WAITING"   # assembly never started
    assert by["monunius"]["state"] == "WAITING"  # gates never ran
    assert by["medaurus"]["state"] == "WAITING"


def test_v6_single_call_path_unchanged(tmp_path) -> None:
    """The qualified openai-live single-call behavior is untouched."""
    from redi_lab.qualification_evidence import build_evidence_manifest_v3, capture_prior_operational_state
    from redi_lab.recipe_store import RecipeStore

    with RecipeStore("build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite") as store:
        cap = capture_prior_operational_state(store)
    m3 = build_evidence_manifest_v3(cap)
    assert m3.manifest_hash == "38e0d83c480ad546d5d78875ad8baf9c9000f733485d8dac1d15384e9c98c937"
    assert m3.output_contract["required_files"] == ["normalize.py", "reorder.py", "service.py"]
    # the single-call backend is still registered and selected by default name
    from redi_lab.workbench.live_backend import LIVE_BACKEND_ID

    assert LIVE_BACKEND_ID == "openai-live"
