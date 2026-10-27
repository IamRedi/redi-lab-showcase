"""REDI Crew identity tests — presentation layer over real state; no faking."""

from pathlib import Path

from redi_lab.workbench.adapter import WorkbenchAdapter
from redi_lab.workbench.crew import CREW, crew_snapshot


def _run_real_build() -> dict:
    adapter = WorkbenchAdapter()
    started = adapter.start_build("Build a pantry inventory tracker (crew demo)")
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=180)
    return adapter.snapshot(started["build_id"])


def test_twelve_personas_defined_with_original_roles() -> None:
    names = {c["name"] for c in CREW}
    assert names == {"Zojz", "Ora", "Agron", "Teuta", "ZANA", "Andinus", "Bindus",
                     "Redon", "Bato", "Monunius", "Medaurus", "Gentius"}
    assert len(CREW) == 12 and len({c["id"] for c in CREW}) == 12


def test_inactive_personas_never_appear_working() -> None:
    snap = {"status": "PENDING", "tasks": [], "ledger_feed": [], "elapsed_seconds": 0}
    crew = crew_snapshot(snap)["crew"]
    for card in crew:
        assert card["state"] == "WAITING", (card["persona"]["name"], card["state"])


def test_active_mapping_follows_real_state() -> None:
    snap = _run_real_build()
    crew = snap["virtutes"]["crew"]
    by_id = {c["persona"]["id"]: c for c in crew}
    # builder personas mapped from REAL dag tasks: normalize→Agron, reorder→Bindus
    tasks = {t["node_id"]: t["status"] for t in snap["tasks"]}
    norm_state = by_id["agron"]["state"]
    expected = {"PENDING": "WAITING", "RUNNING": "WORKING",
                "VERIFIED": "VERIFIED", "FAILED": "FAILED"}[
        next(s for nid, s in tasks.items() if "normalize" in nid)]
    assert norm_state == expected
    assert by_id["bindus"]["state"] == {"PENDING": "WAITING", "RUNNING": "WORKING",
                                        "VERIFIED": "VERIFIED", "FAILED": "FAILED"}[
        next(s for nid, s in tasks.items() if "reorder" in nid)]
    # Zojz mirrors the engine verdict, never independent of it
    assert by_id["zojz"]["state"] == ("VERIFIED" if snap["status"] == "VERIFIED"
                                      else "FAILED" if snap["status"] == "FAILED" else "WORKING")
    # Teuta: no real frontend station in the canonical run -> never active
    assert by_id["teuta"]["state"] == "WAITING"
    # active count consistent with card states
    active = sum(1 for c in crew if c["state"] in ("WORKING", "TESTING", "REVIEWING"))
    assert snap["virtutes"]["active_count"] == active


def test_model_is_secondary_to_persona() -> None:
    snap = _run_real_build()
    for card in snap["virtutes"]["crew"]:
        assert card["persona"]["name"]  # persona identity present
        model = card["worker_model"]
        assert model is None or "gpt" not in card["persona"]["name"].lower()
        if model:
            # model appears only in the secondary technical fields
            assert card["technical"]["worker_model"] == model


def test_final_verdict_engine_authoritative() -> None:
    snap = _run_real_build()
    zojz = next(c for c in snap["virtutes"]["crew"] if c["persona"]["id"] == "zojz")
    assert snap["verdict"] == ("APP READY" if snap["status"] == "VERIFIED" else "APP FAILED")
    assert zojz["state"] == ("VERIFIED" if snap["verdict"] == "APP READY" else "FAILED")
    # technical details expose real gates/costs
    tech = zojz["technical"]
    assert "provider_calls" in tech and "gates" in tech and "cost_usd" in tech


def test_parallel_real_tasks_possible_in_mapping(tmp_path: Path) -> None:
    snap = {"status": "RUNNING", "tasks": [
        {"node_id": "implement_normalize", "status": "RUNNING", "station": "s1"},
        {"node_id": "implement_reorder", "status": "RUNNING", "station": "s2"},
    ], "ledger_feed": [{"entity_type": "RunEvidence", "count": 3}], "elapsed_seconds": 2}
    crew = crew_snapshot(snap)["crew"]
    by_id = {c["persona"]["id"]: c for c in crew}
    assert by_id["agron"]["state"] == "WORKING"
    assert by_id["bindus"]["state"] == "WORKING"  # real parallel builders shown simultaneously
    assert by_id["teuta"]["state"] == "WAITING"   # no real frontend task
