"""VIRTUTES runtime-state semantics tests — evidence-exact; no invented activity."""

from redi_lab.workbench.crew import crew_snapshot

FIRST_LIVE_CALL_EVENTS = [
    "planning_started",
    "scope_resolved",
    "provider_call_started",
    "provider_call_completed",
    "materialized",
    "gate_started:structured_output_schema", "gate_finished:structured_output_schema:pass",
    "gate_started:scope_security", "gate_finished:scope_security:pass",
    "gate_started:public_verification", "gate_finished:public_verification:pass",
    "gate_started:private_verification", "gate_finished:private_verification:pass",
    "gate_started:integration", "gate_finished:integration:pass",
    "gate_started:reviewer", "gate_finished:reviewer:pass",
]

def _by_id(snap):
    return {c["persona"]["id"]: c for c in snap["crew"]}

def test_pending_future_stage_is_waiting():
    """A gate that has not started must show WAITING, never WORKING."""
    snap = crew_snapshot({"status": "RUNNING", "live_events": [
        "planning_started", "scope_resolved",
        "provider_call_started", "provider_call_completed", "materialized"]})
    by = _by_id(snap)
    assert by["monunius"]["state"] == "WAITING"   # scope gate not started yet
    assert by["bato"]["state"] == "WAITING"       # tests not started yet
    assert by["redon"]["state"] == "WAITING"      # integration not started yet
    assert by["medaurus"]["state"] == "WAITING"   # review not started yet
    assert by["gentius"]["state"] == "WAITING"    # release never inferred
    assert snap["active_count"] == 1              # only Zojz (run in progress)


def test_only_currently_executing_stage_is_active():
    snap = crew_snapshot({"status": "RUNNING", "live_events": [
        "planning_started", "scope_resolved", "provider_call_started",
        "provider_call_completed", "materialized",
        "gate_started:scope_security"]})
    by = _by_id(snap)
    assert by["monunius"]["state"] == "WORKING"   # gate genuinely executing now
    assert by["zojz"]["state"] == "WORKING"
    assert by["redon"]["state"] == "WAITING"      # later gates still waiting
    assert by["medaurus"]["state"] == "WAITING"
    assert snap["active_count"] == 2


def test_completed_stages_transition_correctly():
    snap = crew_snapshot({"status": "RUNNING", "live_events": FIRST_LIVE_CALL_EVENTS[:-2] + [
        "gate_started:reviewer"]})
    by = _by_id(snap)
    assert by["monunius"]["state"] == "VERIFIED"   # scope finished pass
    assert by["bato"]["state"] == "VERIFIED"       # public+private finished pass
    assert by["redon"]["state"] == "VERIFIED"      # integration finished pass
    assert by["medaurus"]["state"] == "REVIEWING"  # reviewer genuinely executing
    snap_done = crew_snapshot({"status": "VERIFIED", "live_events": FIRST_LIVE_CALL_EVENTS})
    by2 = _by_id(snap_done)
    assert by2["medaurus"]["state"] == "VERIFIED"
    assert by2["gentius"]["state"] == "VERIFIED"
    assert snap_done["active_count"] == 0          # nothing executing after terminal


def test_failed_gate_marks_its_persona_failed_and_later_stages_waiting():
    events = ["planning_started", "scope_resolved", "provider_call_started",
              "provider_call_completed", "materialized",
              "gate_started:structured_output_schema", "gate_finished:structured_output_schema:pass",
              "gate_started:scope_security", "gate_finished:scope_security:fail"]
    snap = crew_snapshot({"status": "FAILED", "live_events": events})
    by = _by_id(snap)
    assert by["monunius"]["state"] == "FAILED"
    assert by["bato"]["state"] == "WAITING"        # tests never executed
    assert by["redon"]["state"] == "WAITING"
    assert by["medaurus"]["state"] == "WAITING"
    assert by["gentius"]["state"] == "WAITING"     # release stage never began


def test_ora_active_only_during_real_planning():
    mid_planning = crew_snapshot({"status": "RUNNING", "live_events": ["planning_started"]})
    assert _by_id(mid_planning)["ora"]["state"] == "WORKING"
    after_planning = crew_snapshot({"status": "RUNNING", "live_events": [
        "planning_started", "scope_resolved", "provider_call_started"]})
    assert _by_id(after_planning)["ora"]["state"] == "VERIFIED"


def test_first_live_run_shape_semantically_correct():
    """The exact shape of the successful fe9620001f72 live run."""
    snap = crew_snapshot({"status": "VERIFIED", "live_events": FIRST_LIVE_CALL_EVENTS,
                          "active_worker": "gpt-5.6-terra"})
    by = _by_id(snap)
    # during the provider call there are NO per-component builder events:
    assert by["agron"]["state"] == "WAITING"
    assert by["bindus"]["state"] == "WAITING"
    assert by["teuta"]["state"] == "WAITING"
    assert by["zojz"]["state"] == "VERIFIED"
    assert by["monunius"]["state"] == "VERIFIED"
    assert by["redon"]["state"] == "VERIFIED"
    assert by["medaurus"]["state"] == "VERIFIED"
    assert by["gentius"]["state"] == "VERIFIED"
    assert snap["active_count"] == 0


def test_component_events_activate_builders_only_when_real():
    snap = crew_snapshot({"status": "RUNNING", "live_events": [
        "planning_started", "scope_resolved",
        "component_task_started:normalize", "component_task_started:reorder"]})
    by = _by_id(snap)
    assert by["agron"]["state"] == "WORKING"
    assert by["bindus"]["state"] == "WORKING"
    assert by["teuta"]["state"] == "WAITING"   # no frontend task event
    assert snap["active_count"] == 3           # Zojz + the two real builders


def test_model_secondary_and_technical_details_bound() -> None:
    snap = crew_snapshot({"status": "VERIFIED", "live_events": FIRST_LIVE_CALL_EVENTS,
                          "active_worker": "gpt-5.6-terra",
                          "usage": {"input": 321, "output": 722},
                          "cost_usd": "0.0093060"})
    for card in snap["crew"]:
        if card["persona"]["id"] == "zana":
            assert card["state"] == "WAITING"
            assert card["worker_model"] is None
            assert card["technical"]["tokens"] is None
            assert card["technical"]["cost_usd"] == "0"
            continue
        assert card["worker_model"] == "gpt-5.6-terra"
        assert card["technical"]["worker_model"] == "gpt-5.6-terra"
        assert card["technical"]["tokens"] == {"input": 321, "output": 722}
        assert card["technical"]["cost_usd"] == "0.0093060"
