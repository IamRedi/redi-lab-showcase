"""REDI Crew — Albanian/Illyrian presentation identities mapped onto REAL
REDI stations/tasks/events.

Hard rule: a persona is NEVER shown active unless the real engine has a
corresponding active task/station/event.  The mapping below is deterministic
and derived only from persisted state (outcome, replay, ledger feed).
Identity is presentation only: the underlying worker/model is always shown
as secondary text; the final verdict stays engine-authoritative.
"""

from __future__ import annotations

from typing import Any, Mapping

CREW: tuple[dict[str, str], ...] = (
    {"id": "zojz", "name": "Zojz", "role": "Supreme Orchestrator", "symbol": "⚡", "binding": "run"},
    {"id": "ora", "name": "Ora", "role": "Planning / Scheduling", "symbol": "⏳", "binding": "planning"},
    {"id": "agron", "name": "Agron", "role": "Backend / Core Architecture", "symbol": "⌗", "binding": "task:normalize"},
    {"id": "teuta", "name": "Teuta", "role": "Frontend / Product UI", "symbol": "❖", "binding": "task:frontend"},
    {"id": "zana", "name": "ZANA", "role": "Visual Generation / Creative Assets", "symbol": "◈", "binding": "task:visual"},
    {"id": "andinus", "name": "Andinus", "role": "Database / Persistence", "symbol": "⌬", "binding": "persistence"},
    {"id": "bindus", "name": "Bindus", "role": "Data Flow / Networking", "symbol": "≈", "binding": "task:reorder"},
    {"id": "redon", "name": "Redon", "role": "APIs / External Integrations", "symbol": "⇄", "binding": "gate:integration"},
    {"id": "bato", "name": "Bato", "role": "Testing / Debugging / Fixes", "symbol": "✚", "binding": "gate:tests"},
    {"id": "monunius", "name": "Monunius", "role": "Security / Resilience", "symbol": "⛨", "binding": "gate:scope"},
    {"id": "medaurus", "name": "Medaurus", "role": "Final Reviewer / Guardian", "symbol": "◍", "binding": "gate:reviewer"},
    {"id": "gentius", "name": "Gentius", "role": "Integration / Release", "symbol": "✦", "binding": "gate:release"},
)
CREW_BY_ID = {c["id"]: c for c in CREW}
ZANA = CREW_BY_ID["zana"]


def crew_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the 12 crew cards, evidence-exact.

    WAITING = the real stage/task exists or is expected but has not started.
    WORKING/TESTING/REVIEWING = direct persisted evidence the stage is
    currently executing (a live progress event, real DAG task state, or real
    ledger activity).  VERIFIED/FAILED = real completed outcome.
    Pending future gates never infer activity.
    """
    status = snapshot.get("status")
    events = list(snapshot.get("live_events") or [])
    started = lambda g: f"gate_started:{g}" in events  # noqa: E731
    finished = lambda g: next(  # noqa: E731
        (e.split(":")[-1] for e in reversed(events) if e.startswith(f"gate_finished:{g}:")), None)
    component_map = snapshot.get("component_map") or {
        "agron": "normalize", "teuta": "frontend", "bindus": "reorder"}
    component_event = lambda frag: next(  # noqa: E731
        (e for e in events if e.startswith(f"component_task_started:{frag}")), None)
    component_done = lambda frag: any(  # noqa: E731
        e.startswith(f"component_task_completed:{frag}") for e in events)
    component_failed = lambda frag: any(  # noqa: E731
        e.startswith(f"component_task_failed:{frag}") for e in events)
    feed = {e["entity_type"]: e["count"] for e in snapshot.get("ledger_feed", []) or []}
    terminal = status in ("VERIFIED", "FAILED")
    verified = status == "VERIFIED"
    failed = status == "FAILED"
    running = status == "RUNNING" and not terminal

    cards: list[dict[str, Any]] = []

    def card(pid: str, state: str, current: str | None) -> dict[str, Any]:
        return {"persona": CREW_BY_ID[pid], "state": state, "current_task": current}

    if not events and not terminal and not snapshot.get("tasks") and not feed:
        # nothing real has happened yet: everyone WAITING
        cards.extend(card(c["id"], "WAITING", None) for c in CREW)
    elif events:
        # live governed path: exact per-stage evidence
        planning_done = "scope_resolved" in events or any(
            e.startswith("gate_") for e in events)
        ora = ("VERIFIED" if planning_done else
               "WORKING" if "planning_started" in events else "WAITING")
        cards.append(card("zojz", "WORKING" if running else
                          ("VERIFIED" if verified else "FAILED"), "orchestrating factory run"))
        cards.append(card("ora", ora, "planning/scheduling DAG"))
        for pid, frag in component_map.items():
            if frag == "frontend" and not component_event("frontend") and not component_done("frontend"):
                cards.append(card(pid, "WAITING", None))
                continue
            if component_failed(frag):
                cards.append(card(pid, "FAILED", frag))
            elif component_done(frag):
                cards.append(card(pid, "VERIFIED", frag))
            elif component_event(frag):
                cards.append(card(pid, "WORKING", frag))
            else:
                cards.append(card(pid, "WAITING", None))
        retained_count = sum(1 for e in events if e.startswith("artifact_retained:"))
        andinus = ("VERIFIED" if terminal and retained_count else
                   "WORKING" if retained_count else
                   "VERIFIED" if "materialized" in events else "WAITING")
        if component_event("database"):
            andinus = ("FAILED" if component_failed("database") else
                       "VERIFIED" if component_done("database") else "WORKING")
        cards.append(card("andinus", andinus,
                          f"evidence/artifacts ({retained_count} retained)" if retained_count else "artifact retention"))
        monunius = ("VERIFIED" if finished("scope_security") == "pass" else
                    "FAILED" if finished("scope_security") == "fail" else
                    "WORKING" if started("scope_security") else "WAITING")
        cards.append(card("monunius", monunius, "scope security gate"))
        bato = ("FAILED" if any(finished(g) == "fail" for g in ("public_verification", "private_verification")) else
                "VERIFIED" if finished("public_verification") == "pass" and finished("private_verification") == "pass" else
                "TESTING" if started("public_verification") else "WAITING")
        cards.append(card("bato", bato, "deterministic tests"))
        redon = ("VERIFIED" if finished("integration") == "pass" else
                 "FAILED" if finished("integration") == "fail" else
                 "WORKING" if started("integration") else "WAITING")
        cards.append(card("redon", redon, "integration probe"))
        medaurus = ("VERIFIED" if finished("reviewer") == "pass" else
                    "FAILED" if finished("reviewer") == "fail" else
                    "REVIEWING" if started("reviewer") else "WAITING")
        cards.append(card("medaurus", medaurus, "independent final review"))
        assembly_running = ("component_task_started:service" in events
                            and "component_task_completed:service" not in events)
        assembly_done = "component_task_completed:service" in events
        assembly_failed = any(e.startswith("component_task_failed:service") for e in events)
        gentius = ("FAILED" if assembly_failed else
                   "WORKING" if assembly_running else
                   "VERIFIED" if (verified or assembly_done) else
                   "WAITING")  # failed terminal w/o assembly: stage never began
        cards.append(card("gentius", gentius, "integration assembly" if assembly_running else "integration/release"))
    else:
        # dry factory path: derive from real outcome/replay evidence
        tasks = {t["node_id"]: t for t in snapshot.get("tasks", [])}
        tests = snapshot.get("tests") or {}
        rgates = snapshot.get("replay_gates") or {}
        failure_categories = tests.get("failure_categories") or []
        def task_card(pid: str, frag: str):
            node_id = next((nid for nid in tasks if frag in nid), None)
            if node_id is None:
                return card(pid, "WAITING", None)
            state = {"PENDING": "WAITING", "RUNNING": "WORKING",
                     "VERIFIED": "VERIFIED", "FAILED": "FAILED"}[tasks[node_id]["status"]]
            return card(pid, state, node_id)
        cards.append(card("zojz", "WORKING" if running else
                          ("VERIFIED" if verified else "FAILED" if failed else "WAITING"),
                          "orchestrating factory run"))
        ora = "VERIFIED" if snapshot.get("phases") else ("WORKING" if running else "WAITING")
        cards.append(card("ora", ora, "planning/scheduling DAG"))
        andinus = "WORKING" if running and feed else ("VERIFIED" if feed else "WAITING")
        cards.append(card("andinus", andinus, "evidence ledger"))
        cards.append(task_card("agron", "normalize"))
        cards.append(task_card("bindus", "reorder"))
        cards.append(card("teuta", "WAITING", None))
        monunius = ("VERIFIED" if rgates.get("allowed_file_change_scope") else
                    "FAILED" if rgates.get("allowed_file_change_scope") is False else
                    "WAITING")
        cards.append(card("monunius", monunius, "scope security gate"))
        bato = ("VERIFIED" if verified and not failure_categories else
                "FAILED" if failed and failure_categories else
                "TESTING" if running and feed.get("GateResult") else "WAITING")
        cards.append(card("bato", bato, "deterministic tests"))
        redon = ("VERIFIED" if rgates.get("integration_verification") else
                 "FAILED" if rgates.get("integration_verification") is False else
                 "TESTING" if running and feed.get("GateResult") else "WAITING")
        cards.append(card("redon", redon, "integration probe"))
        medaurus = ("VERIFIED" if rgates.get("independent_final_reviewer") else
                    "FAILED" if rgates.get("independent_final_reviewer") is False else
                    "REVIEWING" if running and feed.get("GateResult") else "WAITING")
        cards.append(card("medaurus", medaurus, "independent final review"))
        gentius = ("VERIFIED" if verified else "FAILED" if failed else "WAITING")
        cards.append(card("gentius", gentius, "integration/release"))

    if snapshot.get("backend") == "openai-live-professional-product-v1":
        andinus = next(c for c in cards if c["persona"]["id"] == "andinus")
        andinus["state"] = ("FAILED" if component_failed("database") else
                            "VERIFIED" if component_done("database") else
                            "WORKING" if component_event("database") else "WAITING")
        andinus["current_task"] = "database" if component_event("database") else None
        medaurus = next(c for c in cards if c["persona"]["id"] == "medaurus")
        medaurus["state"] = ("VERIFIED" if "product_review_finished:pass" in events else
                             "FAILED" if failed and "product_review_started" in events else
                             "REVIEWING" if "product_review_started" in events else "WAITING")
        medaurus["current_task"] = "independent product and browser evidence review"
    if snapshot.get('recovery_version'):
        by_persona={c['persona']['id']:c for c in cards}
        for task in (snapshot.get('manifest') or {}).get('tasks',[]):
            state=snapshot.get('task_states',{}).get(task['task_id'],'PENDING')
            c=by_persona.get(task['persona'])
            if c is None:continue
            c['state']={'COMPLETED':'VERIFIED','RUNNING':'WORKING','PENDING':'WAITING'}.get(state,state)
            c['current_task']=task['purpose']
            profile=snapshot.get('recovery_profiles',{}).get(task['task_id'])
            if profile:c['recovery_profile']=profile
        latest=next((e for e in reversed(events) if e.startswith('recovery_')),None)
        if status=='OWNER_ACTION_REQUIRED':
            for c in cards:
                if c['state'] in ('WORKING','VERIFYING','REVIEWING','TESTING','RECOVERY','REPLANNING','ESCALATED'):c['state']='FAILED_INTERNAL'
            by_persona['zojz']['state']='OWNER_ACTION_REQUIRED'
            by_persona['bato']['state']='VERIFIED' if snapshot.get('recovery_episodes') else by_persona['bato']['state']
        elif status=='RUNNING':
            by_persona['zojz']['state']='RECOVERY DECISION' if latest=='recovery_decision_started' else 'RECOVERY' if snapshot.get('app_state')=='RECOVERING' else 'WORKING'
            if latest=='recovery_triage_started':by_persona['bato']['state']='ANALYZING'
            elif latest in ('recovery_diagnosed','recovery_decision_started','recovery_decided','recovery_action_started'):by_persona['bato']['state']='VERIFIED'
            if any(v=='REPLANNING' for v in snapshot.get('task_states',{}).values()):by_persona['ora']['state']='REPLANNING'
    visual_tasks=[t for t in (snapshot.get('manifest') or {}).get('tasks',[]) if t.get('persona')=='zana']
    if visual_tasks:
        task_states=snapshot.get('task_states') or {}
        states=[task_states.get(t['task_id'],'PENDING') for t in visual_tasks]
        optional_skipped=('visual_optional_skipped' in events or
                          any(row.get('event')=='visual_optional_skipped' for row in snapshot.get('events',[]) if isinstance(row,dict)))
        zana_state=('BLOCKED' if (snapshot.get('owner_action') or {}).get('station')=='zana' else
                    'VERIFYING' if 'VERIFYING' in states else
                    'WORKING' if any(s in ('RUNNING','RECOVERY','ESCALATED') for s in states) else
                    'FAILED' if optional_skipped or any(s in ('FAILED','FAILED_INTERNAL') for s in states) else
                    'VERIFIED' if all(s=='COMPLETED' for s in states) else 'WAITING')
        zana_card=next((item for item in cards if item['persona']['id']=='zana'),None)
        if zana_card is None:
            zana_card={'persona':ZANA,'state':'WAITING','current_task':None}
            cards.append(zana_card)
        zana_card['state']=zana_state
        zana_card['current_task']=', '.join(t['task_id'] for t in visual_tasks) if zana_state!='WAITING' else None
    elif not any(item['persona']['id']=='zana' for item in cards):
        cards.append({'persona':ZANA,'state':'WAITING','current_task':None})
    active_states=('WORKING','TESTING','REVIEWING','VERIFYING','RECOVERY','REPLANNING','ESCALATED','ANALYZING','RECOVERY DECISION')
    for card_ in cards:
        is_visual=card_["persona"]["id"]=="zana"
        visual_facts=snapshot.get("visual_facts") or {}
        visual_assets=snapshot.get("visual_assets") or []
        visual_asset=visual_assets[0] if visual_assets else {}
        visual_profile=snapshot.get("visual_worker_profile_record") or {}
        card_["worker_model"] = (visual_asset.get("model") or visual_profile.get("model") or snapshot.get("visual_worker_profile")
                                  if is_visual else
                                  card_.get("recovery_profile",{}).get("model") or snapshot.get("active_worker") or _worker_model(snapshot))
        card_["elapsed_seconds"] = (snapshot.get("elapsed_seconds", 0)
                                    if card_["state"] in active_states else None)
        card_["technical"] = {
            "worker_model": card_["worker_model"],
            "station": "zana" if is_visual else snapshot.get("product_key") or "station_cold_start_implementation_v0",
            "task_id": card_.get("current_task"),
            "provider_calls": visual_facts.get("provider_calls",0) if is_visual else snapshot.get("provider_calls", 0),
            "gates": (snapshot.get("gates") or snapshot.get("replay_gates")) or None,
            "cost_usd": visual_facts.get("cost_usd","0") if is_visual else snapshot.get("cost_usd", "0.00 (dry)"),
            "tokens": None if is_visual else snapshot.get("usage"),
        }
        if is_visual:
            card_["technical"].update(provider=visual_asset.get("provider") or visual_profile.get("provider"),
                                      worker_profile=visual_asset.get("worker_profile") or snapshot.get("visual_worker_profile"),
                                      duration_seconds=visual_facts.get("duration_seconds",0),
                                      reserved_cost_usd=visual_facts.get("reserved_cost_usd","0"),
                                      retries=visual_facts.get("retries",0),
                                      retained_assets=visual_facts.get("retained_assets",0),
                                      accepted_variants=visual_facts.get("accepted_variants",0),
                                      rejected_variants=visual_facts.get("rejected_variants",0))
    active = sum(1 for c in cards if c["state"] in active_states)
    return {"crew": cards, "active_count": active, "total": len(cards)}


def _worker_model(snapshot: Mapping[str, Any]) -> str | None:
    counts = snapshot.get("worker_call_counts") or {}
    if counts:
        return ", ".join(sorted(counts))
    return snapshot.get("active_worker")
