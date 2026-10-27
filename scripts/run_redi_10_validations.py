"""REDI-10A offline validation pack; never invokes a provider.

Default runs the REDI-10A unit proofs.  --all additionally runs the full unit
suite and the existing REDI-01..09 validation packs.  Live runner scripts are
deliberately absent from this list.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKS = tuple(ROOT / "scripts" / f"run_redi_{n:02d}_validations.py" for n in range(1, 10))


def run(*args: str, historical: bool = False) -> bool:
    env = os.environ.copy()
    env["REDI_LIVE_PROVIDER"] = "0"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    for key in ("OPENAI_API_KEY", "REDI_OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env.pop(key, None)
    if historical:
        # REDI-03 launches its own pytest and asserts a clean tracked tree.
        env["PYTEST_ADDOPTS"] = "--basetemp=build/redi10a-historical-pytest --disable-warnings"
    result = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, check=False)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="run full unit suite and REDI-01..09 packs")
    args = parser.parse_args()
    (ROOT / "build").mkdir(exist_ok=True)
    if args.all:
        print("REDI-10A/10B full offline unit suite", flush=True)
        if not run("-m", "pytest", "tests/unit", "-q", "--basetemp=build/redi10-all-pytest"):
            return 1
        for n, pack in enumerate(PACKS, start=1):
            if not pack.is_file():
                raise RuntimeError(f"missing REDI-{n:02d} validation pack: {pack}")
            print(f"REDI-{n:02d} offline validation pack", flush=True)
            if not run(str(pack), historical=True):
                return 1
    else:
        print("REDI-10A/10B offline unit proofs", flush=True)
        pack_tests = sorted(str(p) for p in (ROOT / "tests" / "unit").glob("test_redi10*.py"))
        if not run("-m", "pytest", *pack_tests, "-q", "--basetemp=build/redi10-pack-pytest"):
            return 1
    print("REDI-10A/10B: VERIFIED offline; zero provider/network; no production promotion", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
