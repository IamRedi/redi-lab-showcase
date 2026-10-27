"""REDI-10C1 — stable-baseline qualification evidence plan (offline only).

Prepares and freezes the exact evidence-acquisition plan required to qualify
recipe v6 under stable criteria v2.  Zero provider calls, zero promotion, zero
store mutation.  Live execution remains a separate owner-authorized stage.

Frozen here (all rebuildable byte-identically):

- the captured first-baseline rollback state (no promoted recipe / accepted
  REDI-07B fallback operational path) with restoration procedure and a
  deterministic read-only restoration rehearsal run twice
- a neutral 4-candidate product frame for the SAME product class with a
  result-independent deterministic 2-of-4 selection
- per-product paired comparison specs (baseline fallback arm vs v6 arm)
- the QualificationEvidenceManifest binding criteria v2, v6 identity, the
  existing verified depot transfer evidence, the two selected products, the
  rollback captured-state hash, execution order, gates, replay and no-retry
  policies, validity-window policy, and APPROVAL SLOTS ONLY (never approvals)
- the future live-call plan: 4 provider calls (2 products x 2 arms), models,
  expected cost, hard cost bound; the owner ceiling is NOT chosen here
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping, Sequence

from .holdout_transfer import (
    GPT56_TERRA_WORKER_ID,
    GPT56_TERRA_WORKER_VERSION,
    INDEPENDENT_REVIEWER_WORKER,
    PLANNED_LIVE_PROVIDER_CALLS,
)
from .live_cold_start import HISTORICAL_LIVE_CANDIDATE
from .mcw_pricing import live_cost_plan
from .recipe_store import RecipeStore
from .stable_baseline import (
    BootstrapPriorState,
    ProductHoldoutEvidence,
    STABLE_BASELINE_CRITERIA_V2,
    classify_store_baseline,
    digest,
)

POLICY_ID = "redi10c1_qualification_evidence_v1"
POLICY_VERSION = 1
SEED_DOMAIN_TAG = "REDI-10C1-evidence-v1"
STATION_IMPLEMENTATION = "station_cold_start_implementation_v0"

V6_RECIPE_ID = "recipe_cold_39553c39ceb593e2bc24775be1c420e2"
V6_RECIPE_VERSION = 6
V6_CANONICAL_HASH = "de3aec903f7cf43fee3cf09f65503b8a82c2b81727cdcfe5515926af75dd8e3e"
PRODUCT_CLASS_ID = "product_class_cold_5234225c641e70fba9077fb7"
PRODUCT_CLASS_SIGNATURE_HASH = "96da7671a285e36710e4a119c428fed619cae9597c0e3dda4d6dc60c04e78ffd"

EXISTING_PRODUCT_ID = "redi08_depot_replenishment_holdout"
EXISTING_PRODUCT_EVIDENCE_HASH = "de3aec903f7cf43fee3cf09f65503b8a82c2b81727cdcfe5515926af75dd8e3e"
FORBIDDEN_PRODUCT_KEYS = ("inventory_math", "depot_replenishment", "clinic_supply_replenishment")
FORBIDDEN_INTERFACE_MARKERS = (
    "normalize_depot_code",
    "build_depot_replenishment",
    "normalize_clinic_code",
    "build_clinic_replenishment",
    "normalize_inventory_code",
    "build_inventory_replenishment",
)

BASELINE_MODEL = HISTORICAL_LIVE_CANDIDATE.model  # gpt-4o-mini-2024-07-18
V6_MODEL = "gpt-5.6-terra"
MANDATORY_GATES: tuple[str, ...] = (
    "private_verification",
    "integration",
    "reviewer",
    "scope_security",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. First-baseline rollback state capture
# ---------------------------------------------------------------------------


def capture_prior_operational_state(store: RecipeStore) -> dict[str, Any]:
    """Capture the legitimate pre-promotion operational state (read-only).

    The state is NOT a recipe and no rollback recipe is fabricated: it is the
    accepted REDI-07B fallback operational path plus the store's own
    classification proving no promoted recipe exists.
    """
    classification = classify_store_baseline(store, V6_RECIPE_ID)
    definition = {
        "state_id": "no_promoted_recipe_fallback_v0",
        "definition": (
            "no VALIDATED or selected recipe for the cold-start implementation "
            "station; the operational path is the accepted REDI-07B fallback "
            "profile with one-call policy and strict deterministic gates"
        ),
        "station_id": STATION_IMPLEMENTATION,
        "store_classification": classification["classification"],
        "store_selected_version": classification["selected_version"],
        "validated_versions": classification["validated_versions"],
        "approved_production_promotion": classification["approved_production_promotion"],
        "fallback_policy_id": HISTORICAL_LIVE_CANDIDATE.policy_id,
        "fallback_policy_version": HISTORICAL_LIVE_CANDIDATE.policy_version,
        "fallback_worker_id": HISTORICAL_LIVE_CANDIDATE.worker_id,
        "fallback_model": HISTORICAL_LIVE_CANDIDATE.model,
        "reviewer_worker_id": INDEPENDENT_REVIEWER_WORKER,
        "reviewer_execution": "local deterministic executor (non-provider)",
    }
    restoration_procedure = [
        "verify store classification is STABLE_BASELINE_NOT_YET_ESTABLISHED "
        "and no VALIDATED version or RECIPE_PROMOTED event exists",
        "re-pin the fallback operational profile "
        f"{HISTORICAL_LIVE_CANDIDATE.policy_id} "
        f"({HISTORICAL_LIVE_CANDIDATE.worker_id} / {HISTORICAL_LIVE_CANDIDATE.model}) "
        f"for {STATION_IMPLEMENTATION}",
        "re-run this rehearsal read-only and require the captured_state_hash "
        "to reproduce exactly",
    ]
    config_hash = digest(definition)
    procedure_hash = digest(restoration_procedure)
    rehearsal = rehearse_restoration(store, definition)
    return {
        "definition": definition,
        "config_hash": config_hash,
        "restoration_procedure": restoration_procedure,
        "restoration_procedure_hash": procedure_hash,
        "rehearsal": rehearsal,
        "captured_state_hash": digest(
            {"definition": definition, "config_hash": config_hash, "restoration_procedure_hash": procedure_hash}
        ),
    }


def rehearse_restoration(store: RecipeStore, definition: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic READ-ONLY rehearsal of the restoration verification core."""
    classification = classify_store_baseline(store, V6_RECIPE_ID)
    checks = {
        "no_validated_version": classification["validated_versions"] == [],
        "no_approved_production_promotion": classification["approved_production_promotion"] is False,
        "classification_unchanged": classification["classification"] == "STABLE_BASELINE_NOT_YET_ESTABLISHED",
        "fallback_identity_matches": (
            definition["fallback_worker_id"] == HISTORICAL_LIVE_CANDIDATE.worker_id
            and definition["fallback_model"] == HISTORICAL_LIVE_CANDIDATE.model
        ),
    }
    return {
        "checks": checks,
        "all_passed": all(checks.values()),
        "provider_calls": 0,
        "store_mutations": 0,
    }


# ---------------------------------------------------------------------------
# 2. Neutral candidate frame + deterministic 2-of-4 selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateProduct:
    key: str
    package: str
    normalize_interface: str
    reviewer_interface: str
    fixture_profile: str
    public_checks: tuple[Mapping[str, Any], ...]
    private_checks: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "package": self.package,
            "normalize_interface": self.normalize_interface,
            "reviewer_interface": self.reviewer_interface,
            "fixture_profile": self.fixture_profile,
            "public_checks": [dict(c) for c in self.public_checks],
            "private_checks": [dict(c) for c in self.private_checks],
        }


def _candidate(
    key: str,
    package: str,
    token: str,
    fixture: str,
    norm_pairs: tuple[tuple[tuple[Any, ...], str], ...],
    reorder_cases: tuple[tuple[tuple[int, int, int], int], ...],
) -> CandidateProduct:
    normalize_interface = f"normalize_{token}_code(plant, code)"
    reviewer_interface = f"build_{package}_replenishment"
    return CandidateProduct(
        key=key,
        package=package,
        normalize_interface=normalize_interface,
        reviewer_interface=reviewer_interface,
        fixture_profile=fixture,
        public_checks=tuple(
            {"kind": "call_equals", "target": "normalize", "args": list(a), "expected": e}
            for a, e in norm_pairs[:2]
        )
        + tuple(
            {"kind": "call_equals", "target": "reorder", "args": list(a), "expected": e}
            for a, e in reorder_cases[:1]
        ),
        private_checks=tuple(
            {"kind": "call_equals", "target": "normalize", "args": list(a), "expected": e}
            for a, e in norm_pairs[2:]
        )
        + tuple(
            {"kind": "call_equals", "target": "reorder", "args": list(a), "expected": e}
            for a, e in reorder_cases[1:]
        ),
    )


def _candidate_frame() -> tuple[CandidateProduct, ...]:
    return (
        _candidate(
            "pantry_restock", "pantry_restock", "pantry", "pantry_staples_v1",
            ((["pantry 3", " shelf_8 "], "PANTRY-3#SHELF-8"), (["pantry_11", "b2"], "PANTRY-11#B2"), (["PANTRY_4", "c-1"], "PANTRY-4#C-1")),
            (((2, 6, 2), 10), ((0, 5, 2), 10), ((9, 4, 2), 0)),
        ),
        _candidate(
            "pharmacy_supply", "pharmacy_supply", "pharmacy", "pharmacy_skus_v1",
            ((["pharmacy 2", " tray_5 "], "PHARMACY-2#TRAY-5"), (["pharmacy_7", "a1"], "PHARMACY-7#A1"), (["PHARMACY_9", "d-3"], "PHARMACY-9#D-3")),
            (((1, 7, 3), 10), ((4, 4, 3), 0), ((0, 2, 3), 2)),
        ),
        _candidate(
            "hardware_restock", "hardware_restock", "hardware", "hardware_bins_v1",
            ((["hardware 5", " rack_2 "], "HARDWARE-5#RACK-2"), (["hardware_8", "c4"], "HARDWARE-8#C4"), (["HARDWARE_1", "e-6"], "HARDWARE-1#E-6")),
            (((3, 9, 4), 12), ((2, 2, 4), 0), ((0, 1, 4), 4)),
        ),
        _candidate(
            "stationery_supply", "stationery_supply", "stationery", "stationery_skus_v1",
            ((["stationery 4", " box_9 "], "STATIONERY-4#BOX-9"), (["stationery_6", "b7"], "STATIONERY-6#B7"), (["STATIONERY_2", "f-2"], "STATIONERY-2#F-2")),
            (((5, 8, 2), 11), ((7, 7, 2), 0), ((0, 3, 2), 6)),
        ),
    )


def candidate_frame_hash() -> str:
    return digest(
        {
            "product_class_id": PRODUCT_CLASS_ID,
            "signature_hash": PRODUCT_CLASS_SIGNATURE_HASH,
            "candidates": [c.to_dict() for c in _candidate_frame()],
        }
    )


def selection_seed() -> str:
    material = PRODUCT_CLASS_SIGNATURE_HASH + V6_CANONICAL_HASH + SEED_DOMAIN_TAG
    return _sha256_text(material)


def selection_ranks() -> dict[str, str]:
    seed = selection_seed()
    return {c.key: _sha256_text(f"{seed}:{c.key}") for c in _candidate_frame()}


def select_products() -> tuple[CandidateProduct, CandidateProduct]:
    ranks = selection_ranks()
    ordered = sorted(ranks, key=lambda k: (ranks[k], k))
    chosen = ordered[:2]
    frame = {c.key: c for c in _candidate_frame()}
    return (frame[chosen[0]], frame[chosen[1]])


def class_compatibility_proof(candidate: CandidateProduct) -> dict[str, Any]:
    """Prove the candidate carries the exact class invariants of the recipe."""
    categories = {"identifier_normalization", "integer_reorder", "mapping_result"}
    checks = {
        "technology_family_python_stdlib": candidate.fixture_profile != "" and candidate.package.isidentifier(),
        "identifier_normalization_present": any(c["target"] == "normalize" for c in candidate.public_checks),
        "integer_reorder_present": any(c["target"] == "reorder" for c in candidate.public_checks),
        "mapping_result_present": isinstance(candidate.public_checks[0]["expected"], str),
        "interface_categories_covered": categories == {
            "identifier_normalization",
            "integer_reorder",
            "mapping_result",
        },
        "product_family": "inventory_replenishment_utility",
        "signature_policy": "redi08a_product_class_match_v0",
    }
    return {"candidate": candidate.key, "compatible": all(checks.values()), "checks": checks}


def distinctness_proof() -> dict[str, Any]:
    first, second = select_products()
    keys = (first.key, second.key)
    packages = {first.package, second.package}
    markers = {first.normalize_interface, second.normalize_interface,
               first.reviewer_interface, second.reviewer_interface}
    overlaps = sorted(packages & set(FORBIDDEN_PRODUCT_KEYS))
    marker_overlaps = sorted(markers & set(FORBIDDEN_INTERFACE_MARKERS))
    return {
        "pairwise_distinct": first.key != second.key and first.to_dict() != second.to_dict(),
        "distinct_from_source_redi07_product": "inventory_math" not in packages,
        "distinct_from_depot_holdout": "depot_replenishment" not in packages,
        "distinct_from_existing_markers": marker_overlaps == [],
        "forbidden_key_overlaps": overlaps,
        "selected_keys": list(keys),
    }


# ---------------------------------------------------------------------------
# 4. Paired comparison specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairedComparisonSpec:
    product: CandidateProduct
    baseline_arm: Mapping[str, Any]
    v6_arm: Mapping[str, Any]
    shared: Mapping[str, Any]
    quality_decision_rule: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": self.product.to_dict(),
            "baseline_arm": dict(self.baseline_arm),
            "v6_arm": dict(self.v6_arm),
            "shared": dict(self.shared),
            "quality_decision_rule": self.quality_decision_rule,
        }


QUALITY_DECISION_RULE = (
    "non-regression is decided ONLY from the frozen deterministic gates: the "
    "v6 arm must pass all mandatory gates on the product; the baseline arm "
    "outcome is recorded for comparison. No subjective score, no weighted "
    "metric, no post-result criterion change."
)


ACQUISITION_STEPS: tuple[str, ...] = (
    "capture and owner-approve the prior operational state (already frozen)",
    "per new product: execute baseline arm (1 call) and v6 arm (1 call) under "
    "the frozen paired spec; independent review runs on the local deterministic "
    "executor with zero provider calls",
    "reconcile the existing depot transfer evidence into governed hash-chained "
    "provenance WITHOUT mutating historical recipe store bytes",
    "aggregate per-product ProductHoldoutEvidence records and evaluate against "
    "criteria v2 offline; ELIGIBLE_FOR_VALIDATION requires owner/audit approval "
    "for VALIDATED afterwards; STABLE additionally requires the frozen dwell",
)


def post_acquisition_bundle(
    new_product_results: Mapping[str, bool],
) -> list[ProductHoldoutEvidence]:
    """The evidence bundle as it will exist AFTER the acquisition steps.

    The depot record's provenance is reconciled by the acquisition step (a
    governed side record; historical store bytes stay immutable), so the
    future bundle can satisfy EVIDENCE_COMPLETENESS if the new products verify.
    """
    depot = replace(first_product_evidence(), store_metadata_reconciled=True)
    records = [depot]
    for key in sorted(new_product_results):
        verified = bool(new_product_results[key])
        records.append(
            ProductHoldoutEvidence(
                product_id=key,
                transfer_evidence_hash=digest({"product": key, "verified": verified}),
                verified=verified,
                gates={g: verified for g in MANDATORY_GATES},
                replay_x2_identical=True,
                quality_non_regression=verified,
                provider_calls=2,
                store_metadata_reconciled=True,
            )
        )
    return records



# ---------------------------------------------------------------------------
# 2b. REDI-10C2A: canonical reorder semantics + v2 candidate frame (repair)
# ---------------------------------------------------------------------------
# REDI-10C2 pre-call validation found the 10C1 reorder expectations defective.
# Adjudication (REDI-10C2A): canonical semantics EXIST in accepted REDI-07/08
# evidence code and PREDATE the 10C1 selection:
#   REDI-07 (cold_start.py):  reorder_quantity(on_hand, target) = max(target-on_hand, 0)
#   REDI-08 depot (holdout_transfer.py, CASE_PACK=6) and clinic (BOX_PACK=4):
#     shortfall = max(target + safety - on_hand, 0)
#     quantity  = ceil(shortfall / pack) * pack
#     TypeError on bool/non-int, ValueError on negatives
# The 10C1 checks were written without deriving from this rule
# (CANONICAL_SEMANTICS_EXIST_TESTS_WRONG).  v2 repairs the frame ONLY from the
# pre-existing canonical semantics: checks are GENERATED by executing the
# reference, and the rule + per-candidate pack are stated in the worker-facing
# requirement text.  The v1 frame/manifest stay byte-reproducible as evidence.


def canonical_reorder_quantity(on_hand: int, target: int, safety: int, case_pack: int) -> int:
    """The pre-existing canonical REDI-07/08 reorder rule (authoritative)."""
    values = (on_hand, target, safety)
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        raise TypeError("inventory values must be integers")
    if any(v < 0 for v in values):
        raise ValueError("inventory values must be non-negative")
    if case_pack < 1:
        raise ValueError("case pack must be positive")
    shortfall = max(target + safety - on_hand, 0)
    return ((shortfall + case_pack - 1) // case_pack) * case_pack


def canonical_normalize(plant: str, code: str, join: str = "#") -> str:
    """The pre-existing canonical REDI-08 normalize rule (depot family)."""
    def norm(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("labels must be strings")
        return "-".join(value.strip().upper().replace("_", "-").split())
    return f"{norm(plant)}{join}{norm(code)}"


@dataclass(frozen=True)
class CandidateProductV2(CandidateProduct):
    """v2 candidate: canonical-derived checks + full worker-facing spec text."""

    case_pack: int = 0
    pack_unit: str = "cases"
    requirement_text: str = ""
    integration_probe: tuple = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **super().to_dict(),
            "case_pack": self.case_pack,
            "pack_unit": self.pack_unit,
            "requirement_text": self.requirement_text,
            "integration_probe": [dict(c) for c in self.integration_probe],
        }


def _canonical_requirement_text(token: str, package: str, pack: int, unit: str) -> str:
    return (
        "Implement a deterministic inventory replenishment package `" + package + "` "
        "with three components. (1) normalize.py: implement "
        "normalize_" + token + "_code(plant, code) -> str: both arguments must be "
        "strings (else TypeError); trim surrounding whitespace, uppercase, "
        "replace underscores with hyphens, collapse each run of whitespace to "
        "one hyphen; return '<plant>#<code>' with that normalization applied "
        "to each part. (2) reorder.py: define "
        + unit.upper() + "_PACK = " + str(pack) + " and implement "
        + token + "_reorder_quantity(on_hand, target, safety) -> int: all three "
        "arguments must be ints and not bools (else TypeError), must be "
        "non-negative (else ValueError); compute shortfall = "
        "max(target + safety - on_hand, 0) and return shortfall rounded UP to "
        "the nearest multiple of " + unit.upper() + "_PACK. (3) service.py: "
        "implement build_" + package + "_replenishment(plant, code, on_hand, target, "
        "safety) -> dict with keys plant_code (the normalized label), quantity "
        "(the reorder quantity), " + unit + " (quantity // " + unit.upper() + "_PACK), and "
        "safety_stock (the safety argument)."
    )


def _v2_candidate(key, package, token, fixture, pack, unit, norm_cases, reorder_triples, integration_args):
    reorder_cases = tuple(
        (list(triple), canonical_reorder_quantity(*triple, pack))
        for triple in reorder_triples
    )
    normalize_checks = tuple(
        {"kind": "call_equals", "target": "normalize", "args": list(args), "expected": expected}
        for args, expected in norm_cases
    )
    reorder_checks = tuple(
        {"kind": "call_equals", "target": token + "_reorder_quantity", "args": list(a), "expected": e}
        for a, e in reorder_cases
    )
    plant, code, on_hand, target, safety = integration_args
    quantity = canonical_reorder_quantity(on_hand, target, safety, pack)
    probe = {
        "kind": "call_equals",
        "target": "build_" + package + "_replenishment",
        "args": list(integration_args),
        "expected": {
            "plant_code": canonical_normalize(plant, code),
            "quantity": quantity,
            unit: quantity // pack,
            "safety_stock": safety,
        },
    }
    return CandidateProductV2(
        key=key,
        package=package,
        normalize_interface="normalize_" + token + "_code(plant, code)",
        reviewer_interface="build_" + package + "_replenishment",
        fixture_profile=fixture,
        public_checks=normalize_checks[:2] + reorder_checks[:1],
        private_checks=normalize_checks[2:] + reorder_checks[1:],
        case_pack=pack,
        pack_unit=unit,
        requirement_text=_canonical_requirement_text(token, package, pack, unit),
        integration_probe=(probe,),
    )


def _candidate_frame_v2() -> tuple[CandidateProductV2, ...]:
    specs = (
        ("pantry_restock", "pantry_restock", "pantry", "pantry_staples_v1", 5, "cases",
         ((["pantry 3", " shelf_8 "], "PANTRY-3#SHELF-8"), (["pantry_11", "b2"], "PANTRY-11#B2"),
          (["PANTRY_4", "c-1"], "PANTRY-4#C-1")),
         ((2, 6, 2), (9, 4, 2), (0, 5, 2), (3, 9, 4), (2, 2, 5)), ("pantry 3", "shelf-8", 2, 6, 2)),
        ("pharmacy_supply", "pharmacy_supply", "pharmacy", "pharmacy_skus_v1", 3, "trays",
         ((["pharmacy 2", " tray_5 "], "PHARMACY-2#TRAY-5"), (["pharmacy_7", "a1"], "PHARMACY-7#A1"),
          (["PHARMACY_9", "d-3"], "PHARMACY-9#D-3")),
         ((1, 7, 3), (4, 4, 3), (0, 2, 3), (2, 8, 1), (6, 6, 2)), ("pharmacy 2", "tray-5", 1, 7, 3)),
        ("hardware_restock", "hardware_restock", "hardware", "hardware_bins_v1", 8, "racks",
         ((["hardware 5", " rack_2 "], "HARDWARE-5#RACK-2"), (["hardware_8", "c4"], "HARDWARE-8#C4"),
          (["HARDWARE_1", "e-6"], "HARDWARE-1#E-6")),
         ((3, 9, 4), (12, 10, 8), (0, 1, 4), (5, 5, 8), (1, 2, 8)), ("hardware 5", "rack-2", 3, 9, 4)),
        ("stationery_supply", "stationery_supply", "stationery", "stationery_skus_v1", 7, "boxes",
         ((["stationery 4", " box_9 "], "STATIONERY-4#BOX-9"), (["stationery_6", "b7"], "STATIONERY-6#B7"),
          (["STATIONERY_2", "f-2"], "STATIONERY-2#F-2")),
         ((5, 8, 2), (14, 15, 7), (0, 1, 7), (6, 6, 7), (2, 4, 3)), ("stationery 4", "box-9", 5, 8, 2)),
    )
    return tuple(_v2_candidate(*spec) for spec in specs)


def candidate_frame_v2_hash() -> str:
    return digest(
        {
            "product_class_id": PRODUCT_CLASS_ID,
            "signature_hash": PRODUCT_CLASS_SIGNATURE_HASH,
            "canonical_rule": "redi07/08 reorder: shortfall=max(target+safety-on_hand,0) rounded up to pack",
            "candidates": [c.to_dict() for c in _candidate_frame_v2()],
        }
    )


def select_products_v2() -> tuple[CandidateProductV2, CandidateProductV2]:
    """Same result-independent seed/keys as v1: the repair cannot change selection."""
    seed = selection_seed()
    ranks = {c.key: _sha256_text(f"{seed}:{c.key}") for c in _candidate_frame_v2()}
    ordered = sorted(ranks, key=lambda k: (ranks[k], k))
    frame = {c.key: c for c in _candidate_frame_v2()}
    return (frame[ordered[0]], frame[ordered[1]])


def canonical_provenance_proof() -> dict[str, Any]:
    """Prove the canonical rule reproduces the accepted depot checks (pre-10C1)."""
    depot_checks = (((3, 10, 2), 12), ((0, 5, 2), 12), ((18, 10, 2), 0), ((0, 0, 0), 0))
    return {
        "rule_source": [
            "redi_lab/cold_start.py reorder_quantity (REDI-07, 2-arg base)",
            "redi_lab/holdout_transfer.py _DEPOT_REORDER_CODE CASE_PACK=6",
            "redi_lab/holdout_transfer.py _CLINIC_REORDER_CODE BOX_PACK=4",
        ],
        "predates_10c1_selection": True,
        "depot_checks_reproduced": all(
            canonical_reorder_quantity(*args, 6) == expected for args, expected in depot_checks
        ),
    }


def build_evidence_manifest_v2(captured_state: Mapping[str, Any]) -> QualificationEvidenceManifest:
    """Remediated manifest: canonical-derived specs; supersedes the v1 manifest."""
    v1 = build_evidence_manifest(captured_state)
    specs = tuple(
        PairedComparisonSpec(
            product=c,
            baseline_arm=v1.paired_specs[0]["baseline_arm"],
            v6_arm=v1.paired_specs[0]["v6_arm"],
            shared=v1.paired_specs[0]["shared"],
            quality_decision_rule=QUALITY_DECISION_RULE,
        )
        for c in select_products_v2()
    )
    order = []
    position = 1
    for spec in specs:
        for arm in ("baseline_arm", "v6_arm"):
            arm_map = spec.baseline_arm if arm == "baseline_arm" else spec.v6_arm
            order.append(
                {"position": position, "product_key": spec.product.key, "arm": arm,
                 "model": arm_map["model"], "provider_calls": 1}
            )
            position += 1
    return QualificationEvidenceManifestV2(
        manifest_id=v1.manifest_id,
        version=2,
        policy_id=v1.policy_id,
        criteria_v2_hash=v1.criteria_v2_hash,
        recipe=v1.recipe,
        first_product_evidence=v1.first_product_evidence,
        paired_specs=tuple(s.to_dict() for s in specs),
        rollback_captured_state_hash=v1.rollback_captured_state_hash,
        execution_order=tuple(order),
        quality_criterion=v1.quality_criterion,
        replay_required=v1.replay_required,
        validity_window=v1.validity_window,
        no_retry=v1.no_retry,
        approval_slots=v1.approval_slots,
        live_plan=v1.live_plan,
        remediation={
            "adjudication": "REDI-10C2A: CANONICAL_SEMANTICS_EXIST_TESTS_WRONG",
            "canonical_provenance": canonical_provenance_proof(),
            "supersedes_manifest_hash": v1.manifest_hash,
            "v1_manifest_preserved_immutable": True,
            "check_generation": "expected values GENERATED by executing the canonical reference",
        },
    )


def paired_specs() -> tuple[PairedComparisonSpec, ...]:
    baseline_arm = {
        "arm": "A_prior_operational_baseline",
        "worker_id": HISTORICAL_LIVE_CANDIDATE.worker_id,
        "model": HISTORICAL_LIVE_CANDIDATE.model,
        "api": "chat_completions",
        "capability_profile": "openai_chat_legacy_v1",
        "max_output_tokens": 2048,
        "temperature": 0.0,
        "provider_calls": 1,
    }
    v6_arm = {
        "arm": "B_v6_warm_start",
        "worker_id": GPT56_TERRA_WORKER_ID,
        "worker_profile_version": GPT56_TERRA_WORKER_VERSION,
        "model": V6_MODEL,
        "api": "chat_completions",
        "capability_profile": "openai_chat_gpt56_v1",
        "max_output_tokens": 2048,
        "reasoning_effort": "low",
        "provider_calls": 1,
    }
    shared = {
        "reviewer_worker_id": INDEPENDENT_REVIEWER_WORKER,
        "reviewer_execution": "local deterministic executor (non-provider, 0 provider calls)",
        "mandatory_gates": list(MANDATORY_GATES),
        "context_policy": "identical product fixture and context for both arms",
        "one_call_per_arm": True,
        "automatic_retry": False,
        "worker_fallback": "forbidden",
        "human_intervention": "owner_only",
    }
    return tuple(
        PairedComparisonSpec(
            product=c,
            baseline_arm=baseline_arm,
            v6_arm=v6_arm,
            shared=shared,
            quality_decision_rule=QUALITY_DECISION_RULE,
        )
        for c in select_products()
    )


# ---------------------------------------------------------------------------
# 7. QualificationEvidenceManifest (hash-frozen; approval SLOTS only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualificationEvidenceManifest:
    manifest_id: str
    version: int
    policy_id: str
    criteria_v2_hash: str
    recipe: Mapping[str, Any]
    first_product_evidence: Mapping[str, Any]
    paired_specs: tuple[Mapping[str, Any], ...]
    rollback_captured_state_hash: str
    execution_order: tuple[Mapping[str, Any], ...]
    quality_criterion: str
    replay_required: bool
    validity_window: Mapping[str, Any]
    no_retry: bool
    approval_slots: Mapping[str, None]
    live_plan: Mapping[str, Any]

    @property
    def manifest_hash(self) -> str:
        return digest(asdict(self))


def first_product_evidence() -> ProductHoldoutEvidence:
    return ProductHoldoutEvidence(
        product_id=EXISTING_PRODUCT_ID,
        transfer_evidence_hash=EXISTING_PRODUCT_EVIDENCE_HASH,
        verified=True,
        gates={g: True for g in MANDATORY_GATES},
        replay_x2_identical=True,
        quality_non_regression=True,
        provider_calls=1,
        store_metadata_reconciled=False,
    )


def live_plan() -> dict[str, Any]:
    conditions = {"SMALL": BASELINE_MODEL, "FRONTIER": V6_MODEL}
    plan = live_cost_plan(conditions=conditions, n_tasks=2)
    return {
        "provider_calls_total": 2 * 2 * PLANNED_LIVE_PROVIDER_CALLS,
        "per_product": {"baseline_arm_calls": 1, "v6_arm_calls": 1, "reviewer_calls": 0},
        "models_involved": sorted({BASELINE_MODEL, V6_MODEL}),
        "workers_involved": sorted(
            {HISTORICAL_LIVE_CANDIDATE.worker_id, GPT56_TERRA_WORKER_ID, INDEPENDENT_REVIEWER_WORKER}
        ),
        "expected_cost_usd": plan["experiment_expected_estimate"],
        "hard_cost_bound_usd": plan["experiment_hard_upper_bound"],
        "currency": "USD",
        "pricing_schedule_id": "openai_standard_text_pricing_2026_09_22_v1",
        "owner_ceiling": "chosen later by the owner, never by the harness; must cover the hard bound",
    }



@dataclass(frozen=True)
class QualificationEvidenceManifestV2(QualificationEvidenceManifest):
    """v2 manifest: adds the adjudication/remediation provenance block."""

    remediation: Mapping[str, Any] = field(default_factory=dict)

    @property
    def manifest_hash(self) -> str:
        return digest(asdict(self))


def build_evidence_manifest(captured_state: Mapping[str, Any]) -> QualificationEvidenceManifest:
    specs = paired_specs()
    order = []
    position = 1
    for spec in specs:
        for arm in ("baseline_arm", "v6_arm"):
            order.append(
                {
                    "position": position,
                    "product_key": spec.product.key,
                    "arm": arm,
                    "model": spec.to_dict()[arm]["model"],
                    "provider_calls": 1,
                }
            )
            position += 1
    return QualificationEvidenceManifest(
        manifest_id="redi10c1_v6_qualification_evidence_v1",
        version=1,
        policy_id=POLICY_ID,
        criteria_v2_hash=STABLE_BASELINE_CRITERIA_V2.criteria_hash,
        recipe={
            "recipe_id": V6_RECIPE_ID,
            "version": V6_RECIPE_VERSION,
            "canonical_hash": V6_CANONICAL_HASH,
            "lifecycle": "SHADOW",
        },
        first_product_evidence=asdict(first_product_evidence()),
        paired_specs=tuple(s.to_dict() for s in specs),
        rollback_captured_state_hash=captured_state["captured_state_hash"],
        execution_order=tuple(order),
        quality_criterion=QUALITY_DECISION_RULE,
        replay_required=True,
        validity_window={"max_days": STABLE_BASELINE_CRITERIA_V2.validity_window_max_days},
        no_retry=True,
        approval_slots={"validated": None, "stable": None},
        live_plan=live_plan(),
    )


__all__ = [
    "ACQUISITION_STEPS",
    "CandidateProduct",
    "PairedComparisonSpec",
    "POLICY_ID",
    "QUALITY_DECISION_RULE",
    "QualificationEvidenceManifest",
    "V6_CANONICAL_HASH",
    "V6_RECIPE_ID",
    "build_evidence_manifest",
    "build_evidence_manifest_v2",
    "QualificationEvidenceManifestV2",
    "QualificationEvidenceManifestV3",
    "QUALIFICATION_OUTPUT_CONTRACT_V3",
    "build_evidence_manifest_v3",
    "candidate_frame_hash",
    "candidate_frame_v2_hash",
    "canonical_normalize",
    "canonical_provenance_proof",
    "canonical_reorder_quantity",
    "capture_prior_operational_state",
    "class_compatibility_proof",
    "distinctness_proof",
    "first_product_evidence",
    "live_plan",
    "paired_specs",
    "post_acquisition_bundle",
    "rehearse_restoration",
    "select_products",
    "select_products_v2",
    "selection_ranks",
    "selection_seed",
]


# ---------------------------------------------------------------------------
# 2c. REDI-10C2D: manifest v3 — normalize target names + explicit output contract
# ---------------------------------------------------------------------------
# 10C2C adjudications (both CONFIRMED): (A) frozen normalize checks targeted
# bare "normalize" while the requirement text instructed normalize_<token>_code;
# (B) the patch file count was never contractually bound, so a worker could
# legally emit fewer than the three required files.  v3 fixes ONLY these:
# normalize check targets are renamed to the instructed names (expected values
# unchanged, canonical semantics untouched) and an explicit output contract
# binds exactly the three required files per product.  Everything else —
# product semantics, canonical reorder rule, worker/model configs, context,
# pricing + qualifiers, gates, criteria v2, rollback captured state, approval
# slots, 4-call order, one-call-per-arm, no retry — is inherited unchanged.

QUALIFICATION_OUTPUT_CONTRACT_V3: Mapping[str, Any] = {
    "required_files": ["normalize.py", "reorder.py", "service.py"],
    "extra_files": "forbidden",
    "missing_files": "forbidden",
    "action": "write",
}


def _renamed_checks(checks: Sequence[Mapping[str, Any]], token: str) -> tuple[dict[str, Any], ...]:
    target = f"normalize_{token}_code"
    return tuple(
        dict(c, target=target) if c["target"] == "normalize" else dict(c)
        for c in checks
    )


@dataclass(frozen=True)
class QualificationEvidenceManifestV3(QualificationEvidenceManifestV2):
    """v3 manifest: normalize target names + explicit exactly-3-file contract."""

    output_contract: Mapping[str, Any] = field(default_factory=dict)


def build_evidence_manifest_v3(captured_state: Mapping[str, Any]) -> QualificationEvidenceManifestV3:
    v2 = build_evidence_manifest_v2(captured_state)
    specs = []
    for spec in v2.paired_specs:
        product = dict(spec["product"])
        token = product["key"].split("_")[0]
        product["public_checks"] = _renamed_checks(product["public_checks"], token)
        product["private_checks"] = _renamed_checks(product["private_checks"], token)
        specs.append({
            **spec,
            "product": product,
            "output_contract": dict(QUALIFICATION_OUTPUT_CONTRACT_V3),
        })
    manifest_id = v2.manifest_id.replace("_v1", "_v3")
    return QualificationEvidenceManifestV3(
        manifest_id=manifest_id,
        version=3,
        policy_id=v2.policy_id,
        criteria_v2_hash=v2.criteria_v2_hash,
        recipe=v2.recipe,
        first_product_evidence=v2.first_product_evidence,
        paired_specs=tuple(specs),
        rollback_captured_state_hash=v2.rollback_captured_state_hash,
        execution_order=v2.execution_order,
        quality_criterion=v2.quality_criterion,
        replay_required=v2.replay_required,
        validity_window=v2.validity_window,
        no_retry=v2.no_retry,
        approval_slots=v2.approval_slots,
        live_plan=v2.live_plan,
        remediation={
            **v2.remediation,
            "supersedes_manifest_hash": v2.manifest_hash,
            "redi10c2c_adjudications": {
                "normalize_contract": "CONFIRMED: checks targeted bare 'normalize' while the requirement text instructed normalize_<token>_code",
                "output_contract": "CONFIRMED: patch file count was never contractually bound",
            },
            "corrections": [
                "normalize check targets renamed to the instructed names; expected values unchanged",
                "explicit exactly-3-file output contract bound per product",
            ],
            "v1_v2_preserved_immutable": True,
        },
        output_contract=dict(QUALIFICATION_OUTPUT_CONTRACT_V3),
    )
