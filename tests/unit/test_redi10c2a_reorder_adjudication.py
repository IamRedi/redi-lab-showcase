"""REDI-10C2A reorder adjudication tests — offline; zero provider/network."""

import socket

import pytest

from redi_lab.qualification_evidence import (
    _candidate_frame,
    _candidate_frame_v2,
    build_evidence_manifest,
    build_evidence_manifest_v2,
    canonical_normalize,
    canonical_provenance_proof,
    canonical_reorder_quantity,
    capture_prior_operational_state,
    select_products,
    select_products_v2,
)
from redi_lab.recipe_store import RecipeStore

STORE = "build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite"
V1_MANIFEST_HASH = "56f9bfac2e0a3a8ad62ad73c14f1e9dc7f6a40460282185db1f60eff7ad54cda"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("REDI-10C2A must not use network/provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_canonical_rule_reproduces_accepted_depot_checks() -> None:
    proof = canonical_provenance_proof()
    assert proof["predates_10c1_selection"] is True
    assert proof["depot_checks_reproduced"] is True


def test_v2_checks_generated_from_canonical_reference() -> None:
    for c in _candidate_frame_v2():
        for check in c.public_checks + c.private_checks:
            if check["target"] == "normalize":
                assert canonical_normalize(*check["args"]) == check["expected"]
            else:
                assert canonical_reorder_quantity(*check["args"], c.case_pack) == check["expected"]
        for probe in c.integration_probe:
            plant, code, on_hand, target, safety = probe["args"]
            q = canonical_reorder_quantity(on_hand, target, safety, c.case_pack)
            assert probe["expected"] == {
                "plant_code": canonical_normalize(plant, code),
                "quantity": q,
                c.pack_unit: q // c.case_pack,
                "safety_stock": safety,
            }


def test_requirement_text_states_canonical_rule() -> None:
    for c in _candidate_frame_v2():
        assert "shortfall = max(target + safety - on_hand, 0)" in c.requirement_text
        assert f"_PACK = {c.case_pack}" in c.requirement_text


def test_selection_unchanged_and_v1_evidence_immutable() -> None:
    assert [c.key for c in select_products_v2()] == [c.key for c in select_products()]
    assert len(_candidate_frame()) == 4  # v1 frame intact
    with RecipeStore(STORE) as store:
        cap = capture_prior_operational_state(store)
    assert build_evidence_manifest(cap).manifest_hash == V1_MANIFEST_HASH


def test_v2_manifest_supersedes_v1() -> None:
    with RecipeStore(STORE) as store:
        cap = capture_prior_operational_state(store)
    m2 = build_evidence_manifest_v2(cap)
    assert m2.version == 2
    assert m2.remediation["supersedes_manifest_hash"] == V1_MANIFEST_HASH
    assert m2.remediation["adjudication"] == "REDI-10C2A: CANONICAL_SEMANTICS_EXIST_TESTS_WRONG"
    assert m2.remediation["v1_manifest_preserved_immutable"] is True
    assert m2.approval_slots == {"validated": None, "stable": None}
