"""REDI-10C3 provenance reconciliation tests — offline; zero provider/network."""

import socket
from pathlib import Path

import pytest

from redi_lab.provenance_reconciliation import (
    RECORD_PATH,
    build_reconciliation_record,
    diff_import_only_proof,
    retained_artifact_hashes,
    v6_replay,
    write_record,
)
from redi_lab.recipe_store import RecipeStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("REDI-10C3 must not use network/provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_all_eight_statements_true() -> None:
    s = build_reconciliation_record()["statements"]
    keys = sorted(k for k in s if k[0].isdigit())
    assert len(keys) == 9  # 8 statements + the 7_reason annotation
    for k in keys:
        if k == "7_reason":
            continue
        assert s[k] is True or isinstance(s[k], str), k
    assert s["1_live_execution_code"] == "7521916"
    assert s["2_aggregation_recovery_code"] == "7d8b341"
    assert s["3_diff_only_missing_import"] is True
    assert s["4_execution_path_byte_identical"] is True
    assert s["5_v6_arms_replay_from_retained_bytes_all_gates_pass"] is True
    assert s["6_criteria_v2_met"] is True
    assert s["7_original_live_rows_not_persisted"] is True
    assert s["8_recovered_provenance_not_original_live_rows"] is True


def test_diff_is_import_only_and_mechanical() -> None:
    proof = diff_import_only_proof()
    assert proof["import_only"] is True
    assert proof["execution_path_byte_identical"] is True
    assert proof["changed_lines"] == ["+    first_product_evidence,"]


def test_retained_artifact_hashes_match_disk() -> None:
    from redi_lab.provenance_reconciliation import _sha256_file

    artifacts = retained_artifact_hashes()
    stationery = artifacts["stationery_supply/v6_arm"]["workspace_files"]
    disk = Path("build/redi10c2f-live-acquisition/stationery_supply/v6_arm/stationery_supply/normalize.py")
    assert stationery["stationery_supply/normalize.py"]["sha256"] == _sha256_file(disk)
    assert artifacts["qualification_ledger"]["state"] == "ELIGIBLE_FOR_VALIDATION"
    # baseline arms: outputs not materialized (scope-fail path) — honest limitation
    assert artifacts["stationery_supply/baseline_arm"]["workspace_files"] == {}


def test_v6_replay_x2_identical_and_all_pass() -> None:
    replay = v6_replay()
    assert replay["replay_x2_identical"] is True
    assert replay["both_v6_arms_all_gates_pass"] is True


def test_record_deterministic_and_persisted() -> None:
    a, b = build_reconciliation_record(), build_reconciliation_record()
    assert a["record_hash"] == b["record_hash"]
    persisted = write_record()
    assert persisted["record_hash"] == a["record_hash"]
    on_disk = __import__("json").loads(RECORD_PATH.read_text(encoding="utf-8"))
    assert on_disk["record_hash"] == a["record_hash"]
    assert on_disk["bindings"]["manifest_v3_hash"] == "38e0d83c480ad546d5d78875ad8baf9c9000f733485d8dac1d15384e9c98c937"
    assert on_disk["no_backfill_no_history_edit"] is True
    assert on_disk["provider_calls"] == 0
    assert on_disk["promotion"] == "none"
    # total spend bound
    facts = on_disk["bindings"]["recorded_call_facts"]
    from decimal import Decimal

    assert sum(Decimal(f["cost_usd"]) for f in facts) == Decimal("0.017037000")
