"""Local Workbench browser rehearsal with injected providers and no outbound network.

Each fake call waits for <output>/release-<task_id> so browser observations
can prove actual in-flight transitions. This never constructs a live client.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from redi_lab.workbench import engine
from redi_lab.workbench.normal_app import BACKEND_ID, TASKS, canonical_fake_providers
from redi_lab.workbench.server import serve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    engine.WORKBENCH_ROOT = output / "builds"
    def deny(*a, **kw):
        raise AssertionError("outbound network forbidden in browser rehearsal")
    socket.socket.connect = deny
    socket.create_connection = deny
    calls = []
    providers = canonical_fake_providers()
    class Completions:
        def create(self, **kwargs):
            prompt = kwargs["messages"][1]["content"]
            task = next(t for t in TASKS if f"Implement ONLY {t['artifact']}" in prompt)
            tid = task["task_id"]
            calls.append(tid)
            (output / ("started-" + tid)).write_text("fake call entered", encoding="utf-8")
            deadline = time.monotonic() + 300
            while not (output / ("release-" + tid)).exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("browser rehearsal did not release fake call")
                threading.Event().wait(.1)
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=320, completion_tokens=300,
                prompt_tokens_details=None), choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=providers[tid](prompt)))])
    class Adapter(engine.WorkbenchAdapter):
        def start_build(self, prompt, **kwargs):
            assert kwargs.get("backend") == BACKEND_ID, "this rehearsal supports only CRM"
            kwargs.update(key="offline-injected-key", live_flag="1", client_factory=lambda:
                SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
            return super().start_build(prompt, **kwargs)
    adapter = Adapter()
    server, url = serve(port=0, adapter=adapter)
    (output / "server.json").write_text(json.dumps({"url": url, "real_provider_calls": 0}), encoding="utf-8")
    transitions = []
    seen = None
    try:
        while not (output / "stop").exists():
            for bid in list(adapter._builds):
                snap = adapter.snapshot(bid)
                state = {r["persona"]["id"]: r["state"] for r in snap["virtutes"]["crew"]}
                if state != seen:
                    transitions.append({"ts": time.time(), "crew": state,
                                        "events": snap.get("live_events", []), "status": snap["status"]})
                    seen = state
                if snap["status"] != "RUNNING":
                    (output / "browser_result.json").write_text(json.dumps({
                        "snapshot": snap, "transitions": transitions, "injected_dispatches": calls,
                        "real_provider_calls": 0, "external_network_calls": 0,
                        "transport": "loopback HTTP only"}, indent=2), encoding="utf-8")
            threading.Event().wait(.1)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
