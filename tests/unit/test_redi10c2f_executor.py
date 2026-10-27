"""REDI-10C2F hardened-executor tests — offline; zero provider/network."""

import socket
from pathlib import Path

import pytest

from redi_lab.recipe_store import RecipeStore
from redi_lab.qualification_evidence import (
    QUALIFICATION_OUTPUT_CONTRACT_V3,
    build_evidence_manifest_v3,
    capture_prior_operational_state,
)
from redi_lab.qualification_executor import (
    LIVE_PLAN,
    evaluate_arm,
    executor_sha256,
    materialize,
    pre_live_self_test,
    replay_from_bytes,
    require_self_test_passed,
    scope_check,
)

STORE = "build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite"


def _manifest_product(key: str) -> dict:
    """The authoritative frozen spec: manifest v3 product definition."""
    with RecipeStore(STORE) as store:
        cap = capture_prior_operational_state(store)
    m3 = build_evidence_manifest_v3(cap)
    return next(s["product"] for s in m3.paired_specs if s["product"]["key"] == key)


STATIONERY = _manifest_product("stationery_supply")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("REDI-10C2F must not use network/provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("redi_lab.qualification_executor.OpenAI", None, raising=False)


def test_retention_before_gates_including_scope_failure(tmp_path) -> None:
    """A returned file missing from the contract is still fully retained."""
    patches = {
        "normalize.py": "def normalize_stationery_code(plant, code):\n    return 'X'\n",
        "reorder.py": "STATIONERY_PACK = 7\n",
        "service.py": "from .normalize import normalize_stationery_code\n",
        "extra_note.py": "SHOULD STILL BE RETAINED = True\n",
    }
    retained = materialize(product_key="stationery_supply", arm="v6_arm",
                           model="gpt-5.6-terra", patches=patches)
    assert len(retained.files) == 4
    result = evaluate_arm(retained, STATIONERY, dict(QUALIFICATION_OUTPUT_CONTRACT_V3), ws_root=tmp_path)
    assert result["gates"]["scope_security"] is False
    assert len(result["retained"]["files"]) == 4  # artifacts retained despite gate failure
    for f in result["retained"]["files"]:
        assert f["content"] == patches[f["rel_path"]]


def test_correct_outputs_scope_pass_and_all_gates_pass(tmp_path) -> None:
    from redi_lab.qualification_executor import fake_canonical_outputs

    retained = materialize(product_key="stationery_supply", arm="v6_arm",
                           model="gpt-5.6-terra",
                           patches=fake_canonical_outputs(STATIONERY))
    ok, reason = scope_check(retained, dict(QUALIFICATION_OUTPUT_CONTRACT_V3))
    assert ok and reason == "exact required file set"
    assert len(retained.files) == 3
    result = evaluate_arm(retained, STATIONERY, dict(QUALIFICATION_OUTPUT_CONTRACT_V3), ws_root=tmp_path)
    assert result["all_mandatory_gates_pass"] is True
    assert all(result["gates"][g] for g in result["gates"])


def test_no_package_prefix_invention_in_scope(tmp_path) -> None:
    """Bare-filename contract: prefixed paths are a scope FAIL, never required."""
    from redi_lab.qualification_executor import fake_canonical_outputs

    good = materialize(product_key="stationery_supply", arm="baseline_arm",
                       model="gpt-4o-mini-2024-07-18",
                       patches=fake_canonical_outputs(STATIONERY))
    ok, _ = scope_check(good, dict(QUALIFICATION_OUTPUT_CONTRACT_V3))
    assert ok is True
    prefixed = materialize(product_key="stationery_supply", arm="baseline_arm",
                           model="gpt-4o-mini-2024-07-18",
                           patches={f"stationery_supply/{k}": v for k, v in fake_canonical_outputs(STATIONERY).items()})
    ok2, reason2 = scope_check(prefixed, dict(QUALIFICATION_OUTPUT_CONTRACT_V3))
    assert ok2 is False and "missing" in reason2


def test_failed_output_fully_retained_and_forensically_available(tmp_path) -> None:
    from redi_lab.qualification_executor import fake_canonical_outputs

    broken = fake_canonical_outputs(STATIONERY, break_reorder=True)
    retained = materialize(product_key="stationery_supply", arm="probe", model="gpt-5.6-terra",
                           patches=broken)
    result = evaluate_arm(retained, STATIONERY, dict(QUALIFICATION_OUTPUT_CONTRACT_V3), ws_root=tmp_path)
    assert result["all_mandatory_gates_pass"] is False
    assert result["retained"]["files"][1]["content"] == broken["reorder.py"]
    assert result["artifacts_retained"] is True


def test_replay_x2_from_retained_bytes_only(tmp_path, monkeypatch) -> None:
    from redi_lab.qualification_executor import fake_canonical_outputs

    retained = materialize(product_key="stationery_supply", arm="v6_arm",
                           model="gpt-5.6-terra",
                           patches=fake_canonical_outputs(STATIONERY))
    raw = retained.to_dict()
    one = replay_from_bytes(raw, STATIONERY, dict(QUALIFICATION_OUTPUT_CONTRACT_V3))
    two = replay_from_bytes(raw, STATIONERY, dict(QUALIFICATION_OUTPUT_CONTRACT_V3))
    assert one == two
    assert one["all_mandatory_gates_pass"] is True
    tampered = dict(raw)
    tampered["files"] = [dict(f, content=f["content"] + "\n# tampered\n")
                         for f in raw["files"]]
    with pytest.raises(Exception, match="integrity"):
        replay_from_bytes(tampered, STATIONERY, dict(QUALIFICATION_OUTPUT_CONTRACT_V3))


def test_pre_live_self_test_gates_live_execution() -> None:
    result = pre_live_self_test()
    assert result["self_test_passed"] is True
    assert len(result["arms"]) == 4
    assert all(a["all_mandatory_gates_pass"] and a["retained_files_count"] == 3 for a in result["arms"])
    assert result["failing_arm"]["all_mandatory_gates_pass"] is False
    assert len(result["failing_arm"]["retained"]["files"]) == 3  # failed output fully retained
    assert result["replay_dependencies"]["provider_calls"] == 0
    # after the self-test passes, live execution is permitted by this gate
    require_self_test_passed()


def test_self_test_refusal_when_not_run(monkeypatch) -> None:
    import redi_lab.qualification_executor as qe

    monkeypatch.setattr(qe, "_SELF_TEST", {"passed": False, "result": None})
    with pytest.raises(qe.ExecutorError, match="self-test"):
        require_self_test_passed()


def test_live_plan_frozen() -> None:
    assert LIVE_PLAN["provider_calls_total"] == 4
    assert LIVE_PLAN["order"] == (
        ("stationery_supply", "baseline_arm", "gpt-4o-mini-2024-07-18"),
        ("stationery_supply", "v6_arm", "gpt-5.6-terra"),
        ("pharmacy_supply", "baseline_arm", "gpt-4o-mini-2024-07-18"),
        ("pharmacy_supply", "v6_arm", "gpt-5.6-terra"),
    )
    assert LIVE_PLAN["hard_cost_bound_usd"] == "0.060416000"
    assert len(executor_sha256()) == 64
