"""Offline validation harness: scrub live credentials, block external sockets, save logs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
GUARD = '''import socket
def allowed(host):
    return host in ('127.0.0.1', 'localhost', '::1', None)
original_connect = socket.socket.connect
original_getaddrinfo = socket.getaddrinfo
def connect(self, address):
    if isinstance(address, tuple) and not allowed(address[0]):
        raise RuntimeError('external network forbidden by offline validation harness')
    return original_connect(self, address)
def getaddrinfo(host, *args, **kwargs):
    if not allowed(host):
        raise RuntimeError('external DNS forbidden by offline validation harness')
    return original_getaddrinfo(host, *args, **kwargs)
socket.socket.connect = connect
socket.getaddrinfo = getaddrinfo
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--packs-only", action="store_true")
    parser.add_argument("--unit-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    guard = output / "offline_guard"
    guard.mkdir(exist_ok=True)
    (guard / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
    env = os.environ.copy()
    for name in list(env):
        if "API_KEY" in name.upper() or name.upper() == "REDI_LIVE_PROVIDER":
            env.pop(name)
    env.update(REDI_LIVE_PROVIDER="0", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(guard) + os.pathsep + str(ROOT),
               PYTEST_ADDOPTS="--basetemp=" + str(output / "nested-pytest").replace("\\", "/"))
    # PYTEST_ADDOPTS is shell-like parsed; quote the workspace's spaces.
    env["PYTEST_ADDOPTS"] = '--basetemp="' + str(output / "nested-pytest").replace("\\", "/") + '" --disable-warnings'
    commands = []
    if not args.packs_only:
        commands.append(("unit", ["-m", "pytest", "tests/unit", "-q", "--tb=short", "--disable-warnings",
                                  "--basetemp=" + str(output / "unit-tmp")]))
    if not args.unit_only:
        commands.extend((f"REDI-{n:02}", [f"scripts/run_redi_{n:02}_validations.py"]) for n in range(1, 11))
    results = []
    for name, command in commands:
        start = time.monotonic()
        with (output / (name + ".log")).open("w", encoding="utf-8") as log:
            proc = subprocess.run([sys.executable, "-X", "utf8", *command], cwd=ROOT,
                                  env=env, stdout=log, stderr=subprocess.STDOUT)
        results.append({"name": name, "exit_code": proc.returncode,
                        "seconds": round(time.monotonic() - start, 2)})
        (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(results[-1]), flush=True)
    return int(any(row["exit_code"] for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
