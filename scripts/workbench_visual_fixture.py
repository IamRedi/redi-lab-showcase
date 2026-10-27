"""Isolated, deterministic UI development server. No providers or app launch.

Run with python -m scripts.workbench_visual_fixture --port 8771.
The real Workbench never imports this module or its fixture data.
"""
import argparse
from copy import deepcopy
import threading

from redi_lab.workbench.crew import crew_snapshot
from redi_lab.workbench.server import serve


def fixture_snapshot(scenario="parallel"):
    tasks = [
        {"task_id": "database", "persona": "andinus", "purpose": "Define records and persistence", "dependencies": [], "owned_paths": ["database.py"]},
        {"task_id": "workflow", "persona": "bindus", "purpose": "Validate workflow transitions", "dependencies": [], "owned_paths": ["workflow.py"]},
        {"task_id": "core", "persona": "agron", "purpose": "Connect the business operations", "dependencies": ["database", "workflow"], "owned_paths": ["core.py"]},
        {"task_id": "ui", "persona": "teuta", "purpose": "Render the product experience", "dependencies": ["core"], "owned_paths": ["ui.py"]},
        {"task_id": "service", "persona": "gentius", "purpose": "Assemble the runnable package", "dependencies": ["ui"], "owned_paths": ["service.py"]},
    ]
    if scenario == "large":
        tasks.extend({"task_id": "extended_"+str(i), "persona": ["agron", "bindus", "teuta"][i % 3],
                      "purpose": "Validate a deliberately long task description with dependent customer account and reporting behavior " + str(i),
                      "dependencies": ["service"] if i < 6 else ["extended_"+str(i-6)], "owned_paths": []} for i in range(24))
    events = ["planning_started", "scope_resolved", "component_task_started:database", "component_task_started:workflow"]
    states = {t["task_id"]: "PENDING" for t in tasks}
    states.update(database="RUNNING", workflow="RUNNING")
    status = "RUNNING"
    if scenario == "failed":
        states.update(database="FAILED", workflow="COMPLETED")
        events += ["component_task_failed:database", "component_task_completed:workflow"]
        status = "FAILED"
    if scenario == "blocked":
        states = {t["task_id"]: "PENDING" for t in tasks}
        events = []
        status = "BLOCKED"
    result = {"build_id": SCENARIOS[scenario], "visual_fixture": True,
              "product_name": "Studio operations / visual fixture", "backend": "openai-live-professional-product-v1",
              "execution_mode": "injected_offline", "status": status, "verdict": "APP FAILED" if status == "FAILED" else status,
              "manifest": {"tasks": tasks}, "task_states": states, "live_events": events,
              "events": [{"event": e} for e in events], "provider_calls": 0, "call_facts": {"calls": 0},
              "cost_usd": "0", "elapsed_seconds": None, "can_open_app": False,
              "component_map": {"agron": "core", "teuta": "ui", "bindus": "workflow"},
              "gate_states": {g: "NOT_RUN" for g in ["structured_output_schema", "scope_security", "public_verification", "private_verification", "integration", "reviewer"]},
              "failures": {"database": "IntegrityError: relationship contract did not pass"} if status == "FAILED" else {},
              "detail": "Provider unavailable in this isolated visual fixture." if status == "BLOCKED" else None,
              "prompt": "Create a workflow for a studio. " * (70 if scenario == "large" else 1)}
    result["virtutes"] = crew_snapshot(result)
    return result


SCENARIOS = {"parallel": "0000000000a1", "failed": "0000000000a2", "blocked": "0000000000a3", "large": "0000000000a4"}


class VisualFixtureAdapter:
    def __init__(self):
        self.recorded_requests = []

    def build_ids(self):
        return list(SCENARIOS.values())

    def snapshot(self, build_id):
        scenario = next((s for s, bid in SCENARIOS.items() if bid == build_id), None)
        if not scenario:
            return {"error": "unknown fixture"}
        result = deepcopy(fixture_snapshot(scenario))
        result["fixture_dispatch_count"] = len(self.recorded_requests)
        return result

    def start_build(self, prompt, **kwargs):
        self.recorded_requests.append({"prompt": prompt, **kwargs})
        return {"build_id": SCENARIOS["parallel"]}

    def app_action(self, *_):
        raise ValueError("Visual fixtures cannot launch applications")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    server, url = serve(port=args.port, adapter=VisualFixtureAdapter())
    print(url, flush=True)
    threading.Event().wait()
