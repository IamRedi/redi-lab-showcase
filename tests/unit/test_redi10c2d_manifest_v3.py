"""REDI-10C2D manifest v3 tests — offline; zero provider/network."""

import importlib
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from redi_lab.qualification_evidence import (
    build_evidence_manifest_v2,
    build_evidence_manifest_v3,
    canonical_normalize,
    canonical_reorder_quantity,
    capture_prior_operational_state,
)
from redi_lab.recipe_store import RecipeStore

STORE = "build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite"
V2_HASH = "0d69d86f6ff49d1fcc6c1c85b5ef812edf4df24b2f3adef9a30c8fbf9463b7d9"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("REDI-10C2D must not use network/provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _canonical_fake_outputs(requirement_text: str, product: dict) -> dict[str, str]:
    """Deterministic fake worker output: exactly the 3 required files."""
    token = product["key"].split("_")[0]
    package = product["package"]
    pack = product["case_pack"]
    unit = product["pack_unit"]
    return {
        f"{package}/normalize.py": (
            f"def normalize_{token}_code(plant, code):\n"
            "    def norm(v):\n"
            "        return '-'.join(v.strip().upper().replace('_', '-').split())\n"
            "    return norm(plant) + '#' + norm(code)\n"
        ),
        f"{package}/reorder.py": (
            f"{unit.upper()}_PACK = {pack}\n"
            f"def {token}_reorder_quantity(on_hand, target, safety):\n"
            "    values = (on_hand, target, safety)\n"
            "    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):\n"
            "        raise TypeError('inventory values must be integers')\n"
            "    if any(v < 0 for v in values):\n"
            "        raise ValueError('inventory values must be non-negative')\n"
            "    shortfall = max(target + safety - on_hand, 0)\n"
            f"    return ((shortfall + {unit.upper()}_PACK - 1) // {unit.upper()}_PACK) * {unit.upper()}_PACK\n"
        ),
        f"{package}/service.py": (
            f"from .normalize import normalize_{token}_code\n"
            f"from .reorder import {unit.upper()}_PACK, {token}_reorder_quantity\n\n"
            f"def build_{package}_replenishment(plant, code, on_hand, target, safety):\n"
            f"    quantity = {token}_reorder_quantity(on_hand, target, safety)\n"
            "    return {\n"
            "        'plant_code': normalize_" + token + "_code(plant, code),\n"
            "        'quantity': quantity,\n"
            f"        '{unit}': quantity // {unit.upper()}_PACK,\n"
            "        'safety_stock': safety,\n"
            "    }\n"
        ),
    }


def _gate_namespace(product: dict, files: dict[str, str], tmp_path: Path):
    """Execute the 3-file package the way a correct harness must (package import)."""
    package = product["package"]
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module(f"{package}.service")
        return module, {k: v for k, v in vars(module).items() if not k.startswith("__")}
    finally:
        sys.path.remove(str(tmp_path))
        for name in (package, f"{package}.normalize", f"{package}.reorder", f"{package}.service"):
            sys.modules.pop(name, None)


def test_v3_supersedes_v2_with_both_adjudications(captured_v3=None) -> None:
    with RecipeStore(STORE) as store:
        cap = capture_prior_operational_state(store)
    m2, m3 = build_evidence_manifest_v2(cap), build_evidence_manifest_v3(cap)
    assert m3.version == 3 and m3.manifest_hash != m2.manifest_hash
    assert m3.remediation["supersedes_manifest_hash"] == V2_HASH
    assert set(m3.remediation["redi10c2c_adjudications"]) == {"normalize_contract", "output_contract"}
    assert m3.remediation["v1_v2_preserved_immutable"] is True


def test_normalize_targets_renamed_expected_values_unchanged() -> None:
    with RecipeStore(STORE) as store:
        cap = capture_prior_operational_state(store)
    m2, m3 = build_evidence_manifest_v2(cap), build_evidence_manifest_v3(cap)
    for s2, s3 in zip(m2.paired_specs, m3.paired_specs):
        token = s3["product"]["key"].split("_")[0]
        for checks2, checks3 in ((s2["product"]["public_checks"], s3["product"]["public_checks"]),
                                 (s2["product"]["private_checks"], s3["product"]["private_checks"])):
            for c2, c3 in zip(checks2, checks3):
                assert c2["expected"] == c3["expected"] and c2["args"] == c3["args"]
                want = f"normalize_{token}_code" if c2["target"] == "normalize" else c2["target"]
                assert c3["target"] == want


def test_output_contract_binds_exactly_three_files() -> None:
    with RecipeStore(STORE) as store:
        cap = capture_prior_operational_state(store)
    m3 = build_evidence_manifest_v3(cap)
    contract = m3.output_contract
    assert contract["required_files"] == ["normalize.py", "reorder.py", "service.py"]
    assert contract["extra_files"] == "forbidden" and contract["missing_files"] == "forbidden"
    for spec in m3.paired_specs:
        assert spec["output_contract"] == contract


def test_dry_rehearsal_all_four_arms_pass(tmp_path) -> None:
    with RecipeStore(STORE) as store:
        cap = capture_prior_operational_state(store)
    m3 = build_evidence_manifest_v3(cap)
    results = []
    for spec in m3.paired_specs:
        product = spec["product"]
        files = _canonical_fake_outputs(product["requirement_text"], product)
        # output contract: exactly the 3 required files, no extras
        required = set(spec["output_contract"]["required_files"])
        assert {f.split("/", 1)[1] for f in files} == required
        module, ns = _gate_namespace(product, files, tmp_path / spec["product"]["key"])
        for check in product["public_checks"] + product["private_checks"]:
            assert ns[check["target"]](*check["args"]) == check["expected"], (product["key"], check)
        probe = product["integration_probe"][0]
        assert ns[probe["target"]](*probe["args"]) == probe["expected"]
        results.append(spec["product"]["key"])
    assert results == ["stationery_supply", "pharmacy_supply"]


def test_dry_replay_x2_and_zero_provider(tmp_path, monkeypatch) -> None:
    with RecipeStore(STORE) as store:
        cap = capture_prior_operational_state(store)
    m3 = build_evidence_manifest_v3(cap)
    assert m3.replay_required is True and m3.no_retry is True
    assert m3.approval_slots == {"validated": None, "stable": None}
    assert m3.live_plan["provider_calls_total"] == 4
    assert m3.rollback_captured_state_hash == cap["captured_state_hash"]
    # deterministic rebuild x2
    with RecipeStore(STORE) as store:
        cap2 = capture_prior_operational_state(store)
    assert build_evidence_manifest_v3(cap2).manifest_hash == m3.manifest_hash
