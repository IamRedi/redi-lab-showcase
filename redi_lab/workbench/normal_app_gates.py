"""Isolated CRM gate adapter; frozen qualification gate predicates are unchanged.

Normal-app builds run in a fresh interpreter and a fresh directory. This avoids
cross-build namespace/SQLite state while preserving live gate events, including
the service-import portion of public verification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from ..qualification_executor import GATE_ORDER, RetainedArm, evaluate_arm


def evaluate_normal_app(retained, product, contract, *, ws_root, progress=None):
    ws = Path(ws_root).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    if any(ws.iterdir()):
        raise RuntimeError("normal-app gate workspace must be empty; refusing stale/cross-run state")
    repo = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-I", "-B", "-c",
               "import sys; sys.path.insert(0, " + repr(str(repo)) + "); "
               "from redi_lab.workbench.normal_app_gates import _worker; _worker()"]
    # No provider credentials are needed to evaluate retained Python modules.
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, cwd=ws)
    timer = threading.Timer(30, process.kill)
    timer.start()
    result = None
    try:
        process.stdin.write(json.dumps({"retained": retained.to_dict(), "product": product,
                                       "contract": contract, "ws": str(ws)}))
        process.stdin.close()
        for line in process.stdout:
            row = json.loads(line)
            if "event" in row and progress:
                progress(row["event"])
            if "result" in row:
                result = row["result"]
        code = process.wait()
        if code or result is None:
            raise RuntimeError(f"normal-app gate process failed (exit={code}): {process.stderr.read()[:1000]}")
        return result
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _worker():
    import contextlib
    import io
    import socket
    import tempfile

    payload = json.load(sys.stdin)
    tempfile.tempdir = payload["ws"]
    protocol = sys.stdout
    def forbidden(*args, **kwargs):
        raise RuntimeError("network is forbidden during offline normal-app verification")
    socket.socket.connect = forbidden
    socket.create_connection = forbidden
    states = {name: "NOT_RUN" for name in GATE_ORDER}
    def emit(event):
        parts = event.split(":")
        if parts[0] == "gate_started":
            if states[parts[1]] == "RUNNING":
                return
            states[parts[1]] = "RUNNING"
        elif parts[0] == "gate_finished":
            states[parts[1]] = "PASS" if parts[2] == "pass" else "FAIL"
        print(json.dumps({"event": event}), file=protocol, flush=True)
        if event == "gate_finished:scope_security:pass":
            # evaluate_arm imports service before emitting its public-check event.
            emit("gate_started:public_verification")
    with contextlib.redirect_stdout(io.StringIO()):
        result = evaluate_arm(RetainedArm.from_dict(payload["retained"]), payload["product"],
                              payload["contract"], ws_root=Path(payload["ws"]), progress=emit)
    for gate in GATE_ORDER:
        if states[gate] == "RUNNING":
            emit(f"gate_finished:{gate}:{'pass' if result['gates'][gate] else 'fail'}")
    result["gate_states"] = states
    print(json.dumps({"result": result}), file=protocol, flush=True)
