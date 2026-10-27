"""Replay the retained CRM failure and controlled correction, without provider access."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.unit.test_normal_app_forensic_repair import (
    EXACT_REASON, FIXTURE, corrected_historical, evaluate, historical,
)
from redi_lab.workbench.normal_app import NormalAppMultiStationRunner, canonical_fake_providers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    def deny(*a, **kw):
        raise AssertionError("network/provider forbidden during offline reproduction")
    socket.socket.connect = deny
    socket.create_connection = deny
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURE.iterdir() if p.is_file()}
    failed = evaluate(historical(), root / "historical-shared", isolated=False)
    assert failed["reasons"] == {"public_verification": EXACT_REASON}
    corrected = evaluate(corrected_historical(), root / "corrected-shared", isolated=False)
    assert corrected["all_mandatory_gates_pass"], corrected["reasons"]
    runner = NormalAppMultiStationRunner(fake_providers=canonical_fake_providers(), ws_root=root / "factory")
    factory = runner.execute()
    assert factory["verified"], factory
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURE.iterdir() if p.is_file()}
    assert before == after
    result = {"historical_fixture_hashes": before, "failure": failed,
              "controlled_historical_correction": corrected, "corrected_factory": factory,
              "historical_bytes_unchanged": True, "provider_calls": 0, "network_calls": 0}
    (root / "reproduction.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"failure": failed["reasons"], "correction_passed": True,
                      "factory_passed": factory["verified"], "provider_calls": 0}, indent=2))


if __name__ == "__main__":
    main()
