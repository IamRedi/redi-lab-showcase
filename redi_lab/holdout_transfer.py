"""REDI-08C predeclared holdout warm-start transfer preparation.

A single, warm-only holdout transfer against a NEW same-class sibling that no
prior REDI run has seen.  This is NOT another cold/warm A/B comparison: A/B
comparability was established by REDI-08B.  The module pins an immutable
recipe version, reuses the existing recipe v2 fallback ladder (no new recipe
version), selects the implementation worker deterministically from that
ladder with the recorded owner-paired-002 failure as fallback-exclusion
evidence, and executes the recipe-derived DAG through the strict gate path.

Dry mode exercises the exact future live path with a deterministic fake
provider adapter while making zero provider/network calls.  Nothing here
claims recipe advantage, stability, or generalization, and nothing promotes
the recipe beyond SHADOW.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .context_compiler import (
    CONTEXT_CLASS_PRIOR_FAILURE_EVIDENCE,
    CONTEXT_CLASS_UPSTREAM_HANDOFFS,
    CONTEXT_STRATEGY_SELECTED_V0,
    ContextFirewall,
    compile_context,
)
from .dag_scheduler import dag_snapshot, product_dag_hash
from .domain import (
    AcceptanceCriterion,
    ArtifactRef,
    Budget,
    ContextManifest,
    FailureCategory,
    FailureRecord,
    FactoryRun,
    FactoryRunId,
    GateResult,
    GateResultId,
    GateStatus,
    GateType,
    HandoffManifest,
    LifecycleEvent,
    LifecycleEventType,
    Mode,
    ProductClass,
    ProductClassId,
    ProductId,
    ProductSpec,
    RecipeRung,
    Requirement,
    RiskTier,
    RunEvidence,
    RunStatus,
    Severity,
    StationId,
    TaskId,
    Timing,
    TokenUsage,
    WorkerId,
    domain_to_json,
)
from .persistence import EvidenceLedger, FactoryRunRepository, bootstrap_schema
from .evidence.artifacts import retain_generated_bytes_artifact
from .paired_experiment import (
    MANDATORY_GATES,
    MAX_OUTPUT_TOKENS,
    WORKER_UNIVERSE,
    product_spec_hash,
    prepare_shadow_recipe,
    worker_profiles,
)
from .recipe_store import (
    RecipeStore,
    StoredRecipeVersion,
    canonical_recipe_semantic_hash,
    product_class_signature_hash,
)
from .replay import ReplayReader
from .runtime.contracts import ParsedPatchObservation, PatchEdit, PatchOutputContract
from .runtime.live_provider_adapter import parse_builder_output_object, render_live_request
from .warm_start import (
    ACCOUNTING_SEMANTICS,
    MatchDecision,
    WARM_START_GATE_POLICY_ID,
    WARM_START_GATE_POLICY_VERSION,
    WARM_START_KNOWN_PRODUCT,
    WARM_START_MATCH_POLICY_ID,
    WARM_START_MATCH_POLICY_VERSION,
    compile_warm_start_dag,
    controlled_warm_stations,
    evaluate_warm_start_eligibility,
    inventory_product_class,
    match_product_class,
    original_product_fixture,
    sibling_product_fixture,
)


WARM_TRANSFER_POLICY_ID = "redi08c_holdout_warm_transfer_v0"
WARM_TRANSFER_POLICY_VERSION = 1
HOLDOUT_SELECTION_POLICY_ID = "redi08c_predeclared_holdout_selection_v0"
HOLDOUT_SELECTION_POLICY_VERSION = 1
WORKER_SELECTION_POLICY_ID = "redi08c_recipe_ladder_worker_selection_v0"
WORKER_SELECTION_POLICY_VERSION = 1
REVALIDATION_DECISION_POLICY_ID = "redi08c_reuse_recipe_v2_fallback_ladder_v0"
REVALIDATION_DECISION_POLICY_VERSION = 1
RETRY_POLICY_ID = "redi08c_no_paid_retry_v0"
WORKER_CLASSIFICATION_POLICY_ID = "redi08c_worker_legitimacy_classification_v0"
WORKER_CLASSIFICATION_POLICY_VERSION = 1
BUILDER_PATCH_OUTPUT_POLICY_ID = "redi_builder_patch_contract_v1"
BUILDER_PATCH_OUTPUT_POLICY_VERSION = 1

#: Worker legitimacy classes (REDI-08C2).  Only GENUINE_EXECUTION_WORKERs may
#: carry canonical holdout transfer acceptance evidence.
WORKER_CLASS_GENUINE = "GENUINE_EXECUTION_WORKER"
WORKER_CLASS_TEST_ONLY = "TEST_ONLY_WORKER"
WORKER_CLASS_ORACLE = "REFERENCE_ORACLE"
WORKER_CLASS_INELIGIBLE = "INELIGIBLE"

#: Acceptance semantics (REDI-08C2 section 11).  A fake-adapter dry run is
#: rehearsal only and never counts as recipe transfer evidence.
ACCEPTANCE_HARNESS_REHEARSAL = "HARNESS_REHEARSAL_ONLY"
ACCEPTANCE_GENUINE_TRANSFER = "GENUINE_WORKER_TRANSFER_ACCEPTANCE"

#: Byte-identity pin of the predeclared holdout ProductSpec, recorded in the
#: accepted REDI-08C report before any worker behaviour was observed.
PREDECLARED_HOLDOUT_PRODUCT_SHA256 = "03ac0e36937293dd98a5eefeb0e427547c52e4623299545eb412ed491515f36b"

#: Rendered builder-context hash frozen from the failed genuine run (REDI-08C3);
#: the alternate worker must receive the exact same governed semantic context.
FROZEN_BUILDER_RENDERED_HASH = "acd5b3a5ebe87983e2427e310fd7b6fc1ddd5ba5beb8d2559c33e5dcc6785889"

#: Fixture tree hash frozen from the same run.
FROZEN_FIXTURE_TREE_HASH = "ba0eced98e61ad796aab5cba2edd85b1c6b16c4469085497dcba1065876a3611"

#: Hard live budget for the future owner warm-transfer run.
PLANNED_LIVE_PROVIDER_CALLS = 1

#: Accepted historical recipe identity (authoritative owner-paired-002 record).
HISTORICAL_RECIPE_ID = "recipe_cold_39553c39ceb593e2bc24775be1c420e2"
HISTORICAL_RECIPE_VERSION = 2
HISTORICAL_RECIPE_SEMANTIC_HASH = "31aef4c1727b1712c83e03d53fd372ee112363b47c47c02abcb5e8bd6152b576"
ACCEPTED_SOURCE_FACTORY_RUN_ID = "factory_run_7f21dd99f6b94664be1434e4f6803393"
ACCEPTED_PRODUCT_CLASS_ID = "product_class_cold_5234225c641e70fba9077fb7"

OWNER_PAIRED_002_PAIR_ID = "redi08b-owner-paired-002"
OWNER_PAIRED_002_CANONICAL_HASH = "bb182ec2a36ec28302e6aa80d5fd3cff296a1ddab2bbe39212c6f2304afceb6c"
OWNER_PAIRED_002_SIBLING_HASH = "814710936564020ee7db97823e64d72c481ae8df7d4b9f633da673de99a9e186"
OWNER_PAIRED_002_CLASSIFICATION = "VALID_PAIRED_FAILURE"
OWNER_PAIRED_002_FAILURE_CLASS = "ordinary_worker_implementation_failure"
OWNER_PAIRED_002_FAILED_WORKER = "worker_pair_provider_candidate_v0"
#: The recorded private deterministic verification boundary at which the
#: preferred worker failed the owner-paired-002 sibling.
OWNER_PAIRED_002_FAILED_BOUNDARY = "private_test_execution"
INDEPENDENT_REVIEWER_WORKER = "worker_pair_independent_reviewer_v0"

ALTERNATE_WORKER_REVALIDATION_POLICY_ID = "redi08c4_alternate_worker_revalidation_v0"
ALTERNATE_GENUINE_WORKER_ID = "worker_cold_openai_live_fallback_v0"
ALTERNATE_WORKER_REVALIDATION_POLICY_VERSION = 1

# REDI-08C6 is a bounded authorization for this one additional genuine worker.
# It does not add a global worker preference or make any relative quality/cost
# claim about the model.
GPT56_TERRA_WORKER_ID = "worker_openai_gpt56_terra_holdout_v0"
#: Canonical WorkerProfile version.  Version 1 stays frozen at the REDI-08C6
#: authorization (1024/medium); version 2 (REDI-08C10) changed ONLY the hard
#: completion ceiling 1024 -> 2048; version 3 (REDI-08C12) changed ONLY
#: reasoning_effort medium -> low after two recorded ceiling saturations.
#: Every earlier version remains reconstructible; unknown versions fail closed.
GPT56_TERRA_WORKER_VERSION = 3
GPT56_TERRA_REVALIDATION_POLICY_ID = "redi08c6_gpt56_terra_worker_revalidation_v0"
GPT56_TERRA_REVALIDATION_POLICY_VERSION = 1
#: Frozen completion budgets per WorkerProfile version.  v1 is the exact C6
#: authorization; v2/v3/v4 keep the same 2048 hard ceiling (C10/C12/C14).
TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION = {
    1: MAX_OUTPUT_TOKENS,  # 1024 — REDI-08C6 authorization, immutable
    2: 2048,  # REDI-08C10 hard completion ceiling
    3: 2048,  # REDI-08C12 keeps the C10 ceiling; reasoning effort changes instead
    4: 2048,  # REDI-08C14 keeps the C10 ceiling; execution API changes instead
}
#: Frozen reasoning efforts per WorkerProfile version.  v1/v2 keep the C6/C10
#: "medium"; v3 introduced the C12 revalidation value "low"; v4 (C14) keeps
#: "low" and changes only the execution API.
TERRA_REASONING_EFFORT_BY_PROFILE_VERSION = {
    1: "medium",
    2: "medium",
    3: "low",
    4: "low",
}
#: Recipe-version -> Terra WorkerProfile-version binding.  The binding is
#: explicit and fail-closed: a recipe version without an entry can never
#: silently inherit a different budget or effort from the live registry.
TERRA_PROFILE_VERSION_BY_RECIPE_VERSION = {
    HISTORICAL_RECIPE_VERSION + 2: 1,  # v4 -> profile v1 (1024/medium), C6
    HISTORICAL_RECIPE_VERSION + 3: 2,  # v5 -> profile v2 (2048/medium), C10
    HISTORICAL_RECIPE_VERSION + 4: 3,  # v6 -> profile v3 (2048/low), C12
    HISTORICAL_RECIPE_VERSION + 5: 4,  # v7 -> profile v4 (2048/low/responses), C14
}
COMPLETION_BUDGET_REVALIDATION_POLICY_ID = "redi08c10_completion_budget_revalidation_v0"
COMPLETION_BUDGET_REVALIDATION_POLICY_VERSION = 1
REASONING_REVALIDATION_POLICY_ID = "redi08c12_reasoning_revalidation_v0"
REASONING_REVALIDATION_POLICY_VERSION = 1
#: C13-audited facts of the low-effort Terra run, recorded verbatim in v7
#: provenance.  No capability claim is expressed or implied.
HISTORICAL_TERRA_LOW_SATURATION_RUN = {
    "run_id": "run_c_low_2048",
    "factory_run_id": "factory_run_ea8ccfe83c79412fac481f4bf266c8f4",
    "execution_api": "chat_completions",
    "model": "gpt-5.6-terra",
    "reasoning_effort": "low",
    "max_completion_tokens": 2048,
    "finish_reason": "length",
    "incomplete": True,
    "refusal_present": False,
    "output_tokens": 2048,
    "reasoning_tokens": 2048,
    "visible_answer_content": "none",
}
#: C13 execution-strategy audit verdict, recorded verbatim in v7 provenance.
HISTORICAL_TERRA_EXECUTION_STRATEGY_AUDIT = {
    "chat_completions_saturation_runs": [
        "run_a_medium_1024",
        "run_b_medium_2048",
        "run_c_low_2048",
    ],
    "audit_verdict": "EXECUTION_STRATEGY_REVIEW_REQUIRED",
    "reasoning_effort_verdict": "LOW_EFFORT_NO_MEANINGFUL_CHANGE",
    "saturation_conclusion": "REASONING_SATURATION_PERSISTS_AT_LOW",
    "owner_authorization": (
        "REDI-08C14 owner-authorized single execution-strategy revalidation: "
        "move the execution boundary from the Chat Completions API to the "
        "Responses API for the SAME Terra model, low reasoning effort, and "
        "2048 total output ceiling"
    ),
    "capability_claim_boundary": (
        "No model-capability conclusion; no claim that the Responses API is "
        "superior before the live experiment."
    ),
}
#: The single semantic metadata delta recorded on recipe v7.
RESPONSES_STRATEGY_REVALIDATION_METADATA = {
    "policy": "REDI-08C14",
    "parameter": "execution_api",
    "prior_value": "chat_completions",
    "value": "responses",
    "prior_api_capability_profile": "openai_chat_gpt56_v1",
    "api_capability_profile": "openai_responses_gpt56_v1",
    "reasoning_effort_unchanged": "low",
    "completion_budget_unchanged_tokens": 2048,
    "max_output_tokens_parameter": "max_output_tokens",
    "max_output_tokens_semantics": (
        "reasoning tokens and visible output share the 2048 bound"
    ),
    "conversation_state": "stateless_single_call",
    "worker_profile_id": GPT56_TERRA_WORKER_ID,
    "worker_profile_version": 4,
    "hard_ceiling_not_expected_usage": True,
}
EXECUTION_STRATEGY_REVALIDATION_POLICY_ID = "redi08c14_responses_strategy_revalidation_v0"
EXECUTION_STRATEGY_REVALIDATION_POLICY_VERSION = 1
#: C9-audited facts of the truncated Terra run, recorded verbatim in v5
#: provenance.  No capability claim is expressed or implied.
HISTORICAL_TERRA_TRUNCATED_RUN = {
    "factory_run_id": "factory_run_f4aa3a32e4754bfa94210404c549ccd8",
    "model": "gpt-5.6-terra",
    "reasoning_effort": "medium",
    "max_completion_tokens": 1024,
    "finish_reason": "length",
    "incomplete": True,
    "refusal_present": False,
    "output_tokens": 1024,
    "reasoning_tokens": 1024,
    "visible_answer_content": "none",
    "failure_classification": "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE",
    "historical_verdict": (
        "VALID FAILED TRANSFER ATTEMPT — PROVIDER OUTPUT INCOMPLETE"
    ),
    "budget_audit_verdict": "COMPLETION_BUDGET_REMEDIATION_REQUIRED",
}
#: C11-audited saturation evidence for BOTH medium-effort Terra runs, recorded
#: verbatim in v6 provenance.  No capability downgrade/upgrade claim is made.
HISTORICAL_TERRA_SATURATION_RUNS = {
    "run_a_medium_1024": {
        "factory_run_id": "factory_run_f4aa3a32e4754bfa94210404c549ccd8",
        "reasoning_effort": "medium",
        "max_completion_tokens": 1024,
        "finish_reason": "length",
        "incomplete": True,
        "refusal_present": False,
        "output_tokens": 1024,
        "reasoning_tokens": 1024,
        "visible_answer_content": "none",
    },
    "run_b_medium_2048": {
        "factory_run_id": "factory_run_7e022c35f18e4ee99f68c3a856bcd54d",
        "reasoning_effort": "medium",
        "max_completion_tokens": 2048,
        "finish_reason": "length",
        "incomplete": True,
        "refusal_present": False,
        "output_tokens": 2048,
        "reasoning_tokens": 2048,
        "visible_answer_content": "none",
    },
    "audit_verdict": "REASONING_BUDGET_SATURATION_REPEATED",
    "owner_authorization": (
        "REDI-08C12 owner-authorized controlled reasoning-configuration "
        "revalidation (reasoning_effort medium -> low) on the same worker, "
        "model, and 2048 completion ceiling"
    ),
    "capability_claim_boundary": (
        "This is NOT a worker capability downgrade or upgrade claim."
    ),
}
#: The single semantic metadata delta recorded on recipe v6.
REASONING_REVALIDATION_METADATA = {
    "policy": "REDI-08C12",
    "parameter": "reasoning_effort",
    "prior_value": "medium",
    "value": "low",
    "completion_budget_unchanged_tokens": 2048,
    "worker_profile_id": GPT56_TERRA_WORKER_ID,
    "worker_profile_version": 3,
    "hard_ceiling_not_expected_usage": True,
}
REPEATED_WORKER_FAILURE_AUDIT = "REPEATED_WORKER_IMPLEMENTATION_FAILURE_ON_FROZEN_HOLDOUT"

#: Audit-recorded identity of the genuine holdout live failure (REDI-08C3).
#: Verified against the persisted run result when that artifact is present.
GENUINE_HOLDOUT_FAILURE = {
    "attempt_id": "redi08c_genuine_holdout_attempt",
    "factory_run_id": "factory_run_8342cf4c660a412b8333d5c4ee4fd0d1",
    "failed_worker": "worker_pair_provider_candidate_v0",
    "model": "gpt-4.1-2025-04-14",
    "failed_boundary": "public_test_execution",
    "failure_class": "ordinary_worker_implementation_failure",
    "failure_record_id": "evidence_c0ce3d8eb0184bdc963d4e6f7b87d016",
    "failure_fingerprint": (
        "normalize check mismatch: expected 'DEPOT-7#BIN-42', got 'DEPOT-7#BIN_42'"
    ),
    "holdout_product_spec_sha256": PREDECLARED_HOLDOUT_PRODUCT_SHA256,
    "recipe_semantic_hash": HISTORICAL_RECIPE_SEMANTIC_HASH,
    "source": "audit_recorded_reference",
    "globally_bad_worker_concluded": False,
}

# Immutable evidence references for the second genuine-worker failure recorded
# by REDI-08C5.  These facts stay in orchestration provenance and never enter
# the GPT-5.6 Terra implementation context.
ALTERNATE_GENUINE_HOLDOUT_FAILURE = {
    "attempt_id": "redi08c5_genuine_holdout_attempt",
    "factory_run_id": "factory_run_d0089572571e4bde9e76f9c07e310869",
    "failed_worker": ALTERNATE_GENUINE_WORKER_ID,
    "model": "gpt-4o-mini-2024-07-18",
    "failed_boundary": "public_test_execution",
    "failure_class": "ordinary_worker_implementation_failure",
    "failure_record_id": "failure_92267d4e2a9c4bdca6a44da25b10b822",
    "attempt_evidence_id": "evidence_a517da060cda44a49c04b8ca8782a3b2",
    "failure_fingerprint": (
        "normalize check mismatch: expected 'DEPOT-7#BIN-42', got 'DEPOT-7#BIN_42'"
    ),
    "holdout_product_spec_sha256": PREDECLARED_HOLDOUT_PRODUCT_SHA256,
    "fixture_tree_sha256": FROZEN_FIXTURE_TREE_HASH,
    "rendered_context_sha256": FROZEN_BUILDER_RENDERED_HASH,
    "recipe_configuration_causal": False,
    "globally_bad_worker_concluded": False,
    "source": "redi08c5_persisted_genuine_failure",
}

_INTEGRATION_NODE = "integration_review"

_EXCEPTIONS: Mapping[str, type] = {"ValueError": ValueError, "TypeError": TypeError}


class HoldoutError(RuntimeError):
    """A holdout selection, pinning, execution, or replay invariant failed."""


class HoldoutRetryBlocked(HoldoutError):
    """The single governed holdout execution slot was crossed twice."""


# ---------------------------------------------------------------------------
# Predeclared holdout sibling catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldoutSiblingDefinition:
    """A fully predeclared same-class sibling: spec, fixture, reference work.

    The reference patches exist only so the deterministic fake adapter (dry)
    and the local deterministic executor (live, for non-provider profiles)
    have concrete governed content to emit; verification always re-derives
    behavior from the declared public/private check tables.
    """

    key: str
    product: ProductSpec
    fixture_files: Mapping[str, str]
    reference_patches: Mapping[str, str]
    normalize_module: str
    reorder_module: str
    integration_function: str
    integration_args: tuple[Any, ...]
    integration_expected: Mapping[str, Any]
    case_pack: int
    public_checks: tuple[Mapping[str, Any], ...]
    private_checks: tuple[Mapping[str, Any], ...]
    private_context_markers: tuple[str, ...]
    normalize_public_test: str
    builder_interface_marker: str
    reviewer_interface_marker: str

    @property
    def normalize_rel_path(self) -> str:
        return f"{self.key}/normalize.py"

    @property
    def reorder_rel_path(self) -> str:
        return f"{self.key}/reorder.py"

    @property
    def service_rel_path(self) -> str:
        return f"{self.key}/service.py"

    @property
    def normalize_interface(self) -> str:
        components = self.product.metadata["warm_start_components"]
        return str(next(item for item in components if item["key"] == "normalize")["interface"])


def _classification_metadata(
    package_name: str, interface_names: tuple[str, ...], fixture_profile: str
) -> dict[str, Any]:
    return {
        "policy_id": WARM_START_MATCH_POLICY_ID,
        "policy_version": WARM_START_MATCH_POLICY_VERSION,
        "class_invariants": inventory_product_class("placeholder").signature["class_invariants"],
        "product_variables": {
            "package_name": package_name,
            "interface_names": list(interface_names),
            "fixture_profile": fixture_profile,
        },
    }


def _component_block(
    *,
    key: str,
    goal: str,
    package: str,
    target: str,
    public_test: str,
    interface: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "goal": goal,
        "selected_paths": [f"{package}/{target}", f"tests/{public_test}"],
        "allowed_scope": [package],
        "target_path": f"{package}/{target}",
        "public_test": f"tests/{public_test}",
        "handoff_id": f"handoff_{key}",
        "handoff_type": f"{package}.{target}.v1",
        "interface": interface,
    }


_DEPOT_NORMALIZE_CODE = '''def normalize_depot_code(depot: str, code: str) -> str:
    if not isinstance(depot, str) or not isinstance(code, str):
        raise TypeError("depot and code must be strings")
    depot_value = "-".join(depot.strip().upper().replace("_", "-").split())
    code_value = "-".join(code.strip().upper().replace("_", "-").split())
    if not depot_value or not code_value:
        raise ValueError("depot and code must be non-empty")
    return f"{depot_value}#{code_value}"
'''

_DEPOT_REORDER_CODE = '''CASE_PACK = 6

def depot_reorder_quantity(on_hand: int, target: int, safety: int) -> int:
    values = (on_hand, target, safety)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise TypeError("inventory values must be integers")
    if any(value < 0 for value in values):
        raise ValueError("inventory values must be non-negative")
    shortfall = max(target + safety - on_hand, 0)
    return ((shortfall + CASE_PACK - 1) // CASE_PACK) * CASE_PACK
'''

_DEPOT_SERVICE_CODE = '''from .normalize import normalize_depot_code
from .reorder import CASE_PACK, depot_reorder_quantity

def build_depot_replenishment(depot, code, on_hand, target, safety):
    quantity = depot_reorder_quantity(on_hand, target, safety)
    return {
        "depot_code": normalize_depot_code(depot, code),
        "quantity": quantity,
        "cases": quantity // CASE_PACK,
        "safety_stock": safety,
    }
'''

_CLINIC_NORMALIZE_CODE = '''def normalize_clinic_code(clinic: str, code: str) -> str:
    if not isinstance(clinic, str) or not isinstance(code, str):
        raise TypeError("clinic and code must be strings")
    clinic_value = "-".join(clinic.strip().upper().replace("_", "-").split())
    code_value = "-".join(code.strip().upper().replace("_", "-").split())
    if not clinic_value or not code_value:
        raise ValueError("clinic and code must be non-empty")
    return f"{clinic_value}@{code_value}"
'''

_CLINIC_REORDER_CODE = '''BOX_PACK = 4

def clinic_reorder_quantity(on_hand: int, target: int, safety: int) -> int:
    values = (on_hand, target, safety)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise TypeError("stock values must be integers")
    if any(value < 0 for value in values):
        raise ValueError("stock values must be non-negative")
    shortfall = max(target + safety - on_hand, 0)
    return ((shortfall + BOX_PACK - 1) // BOX_PACK) * BOX_PACK
'''

_CLINIC_SERVICE_CODE = '''from .normalize import normalize_clinic_code
from .reorder import BOX_PACK, clinic_reorder_quantity

def build_clinic_replenishment(clinic, code, on_hand, target, safety):
    quantity = clinic_reorder_quantity(on_hand, target, safety)
    return {
        "clinic_code": normalize_clinic_code(clinic, code),
        "quantity": quantity,
        "boxes": quantity // BOX_PACK,
        "safety_stock": safety,
    }
'''


def _depot_definition(product_class_id: str) -> HoldoutSiblingDefinition:
    package = "depot_replenishment"
    components = [
        _component_block(
            key="normalize",
            goal=(
                "Implement the exact public interface normalize_depot_code(depot, code) "
                "for depot-namespaced bin-code normalization."
            ),
            package=package,
            target="normalize.py",
            public_test="test_depot_normalize_public.py",
            interface="normalize_depot_code(depot: str, code: str) -> str",
        ),
        _component_block(
            key="reorder",
            goal="Implement case-packed depot reorder quantity with a configurable safety-stock floor.",
            package=package,
            target="reorder.py",
            public_test="test_depot_reorder_public.py",
            interface="depot_reorder_quantity(on_hand: int, target: int, safety: int) -> int",
        ),
    ]
    product = ProductSpec(
        id=ProductId("product_redi08c_holdout_depot_replenishment"),
        requested_outcome=(
            "Build a depot-aware replenishment utility that normalizes depot bin codes "
            "and orders whole case-packed quantities with safety stock."
        ),
        requirements=(
            Requirement(text="Normalize depot bin codes while preserving the depot namespace.", kind="functional"),
            Requirement(
                text="Calculate case-packed reorder quantities including a configurable safety-stock floor.",
                kind="functional",
            ),
            Requirement(text="Integrate both capabilities behind build_depot_replenishment.", kind="functional"),
        ),
        constraints=(
            Requirement(text="Use the Python standard library only.", kind="constraint"),
            Requirement(text="Operate offline on deterministic fixture data.", kind="constraint"),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(description="Depot namespace is present in normalized output."),
            AcceptanceCriterion(description="Quantities are whole case packs without negative values."),
            AcceptanceCriterion(description="Independent integration review passes."),
        ),
        risk_tier=RiskTier.SYNTHETIC,
        requested_artifacts=(f"{package} package", "holdout warm-transfer verification evidence"),
        product_class_id=ProductClassId(product_class_id),
        metadata={
            "product_type": "python_inventory_package",
            "redi08_classification": _classification_metadata(
                package_name=package,
                interface_names=("normalize_depot_code", "depot_reorder_quantity", "build_depot_replenishment"),
                fixture_profile="depot_bin_case_packed_with_safety_stock",
            ),
            "cold_start_components": components,
            "warm_start_components": components,
            "integration": {
                "goal": "Integrate depot normalization and case-packed reorder results.",
                "selected_paths": [f"{package}/service.py", "tests/test_depot_integration_public.py"],
                "allowed_scope": [package, "tests"],
                "interface": "build_depot_replenishment(depot, code, on_hand, target, safety) -> mapping",
            },
        },
    )
    fixture_files = {
        f"{package}/__init__.py": "",
        f"{package}/normalize.py": "# governed holdout baseline\n",
        f"{package}/reorder.py": "# governed holdout baseline\n",
        f"{package}/service.py": "# governed holdout baseline\n",
        "tests/test_depot_normalize_public.py": (
            "# Public contract: normalize_depot_code(depot, code)\n"
            "# Expected example: ('depot 7', ' bin_42 ') -> 'DEPOT-7#BIN-42'\n"
        ),
        "tests/test_depot_reorder_public.py": (
            "# Public contract: case-packed reorder quantity\n"
            "# Expected example: (0, 5, 2) -> 12 (shortfall 7 rounds up to two case packs of 6)\n"
        ),
        "tests/test_depot_integration_public.py": (
            "# Governed holdout integration contract\n"
            "# Expected example: ('depot 7', ' bin_42 ', 0, 5, 2) -> mapping with quantity 12\n"
        ),
    }
    return HoldoutSiblingDefinition(
        key=package,
        product=product,
        fixture_files=fixture_files,
        reference_patches={
            f"{package}/normalize.py": _DEPOT_NORMALIZE_CODE,
            f"{package}/reorder.py": _DEPOT_REORDER_CODE,
            f"{package}/service.py": _DEPOT_SERVICE_CODE,
        },
        normalize_module="normalize_depot_code",
        reorder_module="depot_reorder_quantity",
        integration_function="build_depot_replenishment",
        integration_args=("depot 7", " bin_42 ", 0, 5, 2),
        integration_expected={
            "depot_code": "DEPOT-7#BIN-42",
            "quantity": 12,
            "cases": 2,
            "safety_stock": 2,
        },
        case_pack=6,
        public_checks=(
            {
                "kind": "call_equals",
                "target": "normalize",
                "args": ["depot 7", " bin_42 "],
                "expected": "DEPOT-7#BIN-42",
            },
            {"kind": "call_equals", "target": "reorder", "args": [0, 5, 2], "expected": 12},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "normalize", "args": ["DEPOT_3", "c-9"], "expected": "DEPOT-3#C-9"},
            {"kind": "call_raises", "target": "normalize", "args": ["depot", ""], "exception": "ValueError"},
            {"kind": "call_raises", "target": "normalize", "args": [7, "bin"], "exception": "TypeError"},
            {"kind": "call_equals", "target": "reorder", "args": [3, 10, 2], "expected": 12},
            {"kind": "call_equals", "target": "reorder", "args": [18, 10, 2], "expected": 0},
            {"kind": "call_equals", "target": "reorder", "args": [0, 0, 0], "expected": 0},
            {"kind": "call_raises", "target": "reorder", "args": [True, 1, 1], "exception": "TypeError"},
            {"kind": "call_raises", "target": "reorder", "args": [-1, 1, 1], "exception": "ValueError"},
        ),
        private_context_markers=(
            "DEPOT-3#C-9",
            "DEPOT_3",
            json.dumps([3, 10, 2]),
            json.dumps([18, 10, 2]),
        ),
        normalize_public_test="test_depot_normalize_public",
        builder_interface_marker="normalize_depot_code(depot, code)",
        reviewer_interface_marker="build_depot_replenishment",
    )


def _clinic_definition(product_class_id: str) -> HoldoutSiblingDefinition:
    package = "clinic_supply_replenishment"
    components = [
        _component_block(
            key="normalize",
            goal=(
                "Implement the exact public interface normalize_clinic_code(clinic, code) "
                "for clinic-namespaced supply-code normalization."
            ),
            package=package,
            target="normalize.py",
            public_test="test_clinic_normalize_public.py",
            interface="normalize_clinic_code(clinic: str, code: str) -> str",
        ),
        _component_block(
            key="reorder",
            goal="Implement box-packed clinic reorder quantity with a configurable safety-stock floor.",
            package=package,
            target="reorder.py",
            public_test="test_clinic_reorder_public.py",
            interface="clinic_reorder_quantity(on_hand: int, target: int, safety: int) -> int",
        ),
    ]
    product = ProductSpec(
        id=ProductId("product_redi08c_holdout_clinic_supply_replenishment"),
        requested_outcome=(
            "Build a clinic supply replenishment utility that normalizes clinic supply codes "
            "and orders whole box-packed quantities with safety stock."
        ),
        requirements=(
            Requirement(text="Normalize clinic supply codes while preserving the clinic namespace.", kind="functional"),
            Requirement(
                text="Calculate box-packed reorder quantities including a configurable safety-stock floor.",
                kind="functional",
            ),
            Requirement(text="Integrate both capabilities behind build_clinic_replenishment.", kind="functional"),
        ),
        constraints=(
            Requirement(text="Use the Python standard library only.", kind="constraint"),
            Requirement(text="Operate offline on deterministic fixture data.", kind="constraint"),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(description="Clinic namespace is present in normalized output."),
            AcceptanceCriterion(description="Quantities are whole boxes without negative values."),
            AcceptanceCriterion(description="Independent integration review passes."),
        ),
        risk_tier=RiskTier.SYNTHETIC,
        requested_artifacts=(f"{package} package", "holdout warm-transfer verification evidence"),
        product_class_id=ProductClassId(product_class_id),
        metadata={
            "product_type": "python_inventory_package",
            "redi08_classification": _classification_metadata(
                package_name=package,
                interface_names=("normalize_clinic_code", "clinic_reorder_quantity", "build_clinic_replenishment"),
                fixture_profile="clinic_supply_box_packed_with_safety_stock",
            ),
            "cold_start_components": components,
            "warm_start_components": components,
            "integration": {
                "goal": "Integrate clinic normalization and box-packed reorder results.",
                "selected_paths": [f"{package}/service.py", "tests/test_clinic_integration_public.py"],
                "allowed_scope": [package, "tests"],
                "interface": "build_clinic_replenishment(clinic, code, on_hand, target, safety) -> mapping",
            },
        },
    )
    fixture_files = {
        f"{package}/__init__.py": "",
        f"{package}/normalize.py": "# governed holdout baseline\n",
        f"{package}/reorder.py": "# governed holdout baseline\n",
        f"{package}/service.py": "# governed holdout baseline\n",
        "tests/test_clinic_normalize_public.py": (
            "# Public contract: normalize_clinic_code(clinic, code)\n"
            "# Expected example: ('clinic 5', ' ward_3 ') -> 'CLINIC-5@WARD-3'\n"
        ),
        "tests/test_clinic_reorder_public.py": (
            "# Public contract: box-packed reorder quantity\n"
            "# Expected example: (0, 5, 2) -> 8 (shortfall 7 rounds up to two boxes of 4)\n"
        ),
        "tests/test_clinic_integration_public.py": "# Governed holdout integration contract\n",
    }
    return HoldoutSiblingDefinition(
        key=package,
        product=product,
        fixture_files=fixture_files,
        reference_patches={
            f"{package}/normalize.py": _CLINIC_NORMALIZE_CODE,
            f"{package}/reorder.py": _CLINIC_REORDER_CODE,
            f"{package}/service.py": _CLINIC_SERVICE_CODE,
        },
        normalize_module="normalize_clinic_code",
        reorder_module="clinic_reorder_quantity",
        integration_function="build_clinic_replenishment",
        integration_args=("clinic 5", " ward_3 ", 0, 5, 2),
        integration_expected={
            "clinic_code": "CLINIC-5@WARD-3",
            "quantity": 8,
            "boxes": 2,
            "safety_stock": 2,
        },
        case_pack=4,
        public_checks=(
            {
                "kind": "call_equals",
                "target": "normalize",
                "args": ["clinic 5", " ward_3 "],
                "expected": "CLINIC-5@WARD-3",
            },
            {"kind": "call_equals", "target": "reorder", "args": [0, 5, 2], "expected": 8},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "normalize", "args": ["CLINIC_2", "s-1"], "expected": "CLINIC-2@S-1"},
            {"kind": "call_raises", "target": "normalize", "args": ["", "ward"], "exception": "ValueError"},
            {"kind": "call_equals", "target": "reorder", "args": [3, 10, 2], "expected": 12},
            {"kind": "call_equals", "target": "reorder", "args": [20, 10, 2], "expected": 0},
            {"kind": "call_raises", "target": "reorder", "args": [False, 1, 1], "exception": "TypeError"},
        ),
        private_context_markers=(
            "CLINIC-2@S-1",
            "CLINIC_2",
            json.dumps([3, 10, 2]),
            json.dumps([20, 10, 2]),
        ),
        normalize_public_test="test_clinic_normalize_public",
        builder_interface_marker="normalize_clinic_code(clinic, code)",
        reviewer_interface_marker="build_clinic_replenishment",
    )


def holdout_candidate_catalog(product_class_id: str) -> tuple[HoldoutSiblingDefinition, ...]:
    """Predeclared, ordered holdout candidates; the rule is fixed before execution."""

    return (_depot_definition(product_class_id), _clinic_definition(product_class_id))


# ---------------------------------------------------------------------------
# Predeclared deterministic holdout selection
# ---------------------------------------------------------------------------


def prior_sibling_registry(
    *,
    product_class_id: str,
    owner_paired_002_record: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Product-spec hashes of every sibling product prior REDI runs executed."""

    registry = {
        "redi07_source_product": product_spec_hash(original_product_fixture(product_class_id)),
        "redi08a_sibling_and_redi08b_owner_paired_sibling": product_spec_hash(
            sibling_product_fixture(product_class_id)
        ),
    }
    if owner_paired_002_record is not None:
        recorded = str(owner_paired_002_record.get("sibling_product_spec_hash") or "")
        if recorded:
            registry["redi08b_owner_paired_002_recorded"] = recorded
    return registry


def _default_artifacts_root() -> Path:
    return Path(__file__).resolve().parents[1] / "artifacts" / "redi08b"


def load_owner_paired_002_record(artifacts_root: Path | str | None = None) -> dict[str, Any]:
    """Ground the owner-paired-002 reference in the persisted artifact if present.

    The historical paired result is never rewritten; when its canonical hash
    matches the audited record the persisted classification and sibling hash
    are cited, otherwise the audit-recorded reference stands alone.
    """

    record = {
        "pair_id": OWNER_PAIRED_002_PAIR_ID,
        "classification": OWNER_PAIRED_002_CLASSIFICATION,
        "failure_class": OWNER_PAIRED_002_FAILURE_CLASS,
        "failed_worker": OWNER_PAIRED_002_FAILED_WORKER,
        "failed_boundary": OWNER_PAIRED_002_FAILED_BOUNDARY,
        "recipe_id": HISTORICAL_RECIPE_ID,
        "recipe_version": HISTORICAL_RECIPE_VERSION,
        "recipe_semantic_hash": HISTORICAL_RECIPE_SEMANTIC_HASH,
        "sibling_product_spec_hash": OWNER_PAIRED_002_SIBLING_HASH,
        "canonical_hash": OWNER_PAIRED_002_CANONICAL_HASH,
        "source": "audit_recorded_reference",
        "globally_bad_worker_concluded": False,
        "transfer_failure": False,
        "recipe_configuration_causal": False,
    }
    root = Path(artifacts_root) if artifacts_root is not None else _default_artifacts_root()
    artifact = root / OWNER_PAIRED_002_PAIR_ID / "pair" / "paired_result.json"
    if artifact.exists():
        try:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return record
        if payload.get("canonical_hash") == OWNER_PAIRED_002_CANONICAL_HASH:
            record["sibling_product_spec_hash"] = str(payload.get("sibling_product_spec_hash"))
            record["classification"] = str(payload.get("classification"))
            record["source"] = "persisted_artifact_verified"
    return record


@dataclass(frozen=True)
class HoldoutSelection:
    definition: HoldoutSiblingDefinition
    match: Mapping[str, Any]
    product_spec_sha256: str
    selected_candidate_key: str
    skipped_candidates: tuple[Mapping[str, Any], ...]
    prior_used_registry: Mapping[str, str]
    policy_id: str = HOLDOUT_SELECTION_POLICY_ID
    policy_version: int = HOLDOUT_SELECTION_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "creation_policy": (
                "predeclared ordered catalog fixed in code before execution; the first "
                "candidate that is class-compatible and absent from the prior-sibling "
                "registry is selected; no result-dependent fixture changes"
            ),
            "selected_candidate_key": self.selected_candidate_key,
            "holdout_product_id": str(self.definition.product.id),
            "holdout_product_spec_sha256": self.product_spec_sha256,
            "skipped_candidates": [dict(item) for item in self.skipped_candidates],
            "prior_used_registry": dict(self.prior_used_registry),
            "match": dict(self.match),
        }


def select_holdout_sibling(
    *,
    product_class: ProductClass,
    prior_used_registry: Mapping[str, str],
) -> HoldoutSelection:
    """Deterministic predeclared rule: first unseen, class-compatible candidate."""

    skipped: list[dict[str, Any]] = []
    seen_hashes = set(prior_used_registry.values())
    for definition in holdout_candidate_catalog(str(product_class.id)):
        match = match_product_class(definition.product, product_class)
        product_hash = product_spec_hash(definition.product)
        if match.decision is not MatchDecision.COMPATIBLE:
            skipped.append({"candidate": definition.key, "reason": f"class match is {match.decision.value}"})
            continue
        if product_hash in seen_hashes:
            skipped.append({
                "candidate": definition.key,
                "reason": "candidate was already executed by a prior REDI run (adaptation-leakage guard)",
            })
            continue
        return HoldoutSelection(
            definition=definition,
            match=match.to_dict(),
            product_spec_sha256=product_hash,
            selected_candidate_key=definition.key,
            skipped_candidates=tuple(skipped),
            prior_used_registry=dict(prior_used_registry),
        )
    raise HoldoutError("no unseen class-compatible holdout candidate remains in the predeclared catalog")


def holdout_difference_summary(
    definition: HoldoutSiblingDefinition, product_class: ProductClass
) -> dict[str, Any]:
    """Structured proof the holdout differs materially from every prior sibling."""

    source = original_product_fixture(str(product_class.id))
    paired = sibling_product_fixture(str(product_class.id))

    def _variables(product: ProductSpec) -> dict[str, Any]:
        return dict(product.metadata["redi08_classification"]["product_variables"])

    holdout_variables = _variables(definition.product)
    vs_source = {
        key: {"other": _variables(source).get(key), "holdout": holdout_variables.get(key)}
        for key in sorted(set(_variables(source)) | set(holdout_variables))
        if _variables(source).get(key) != holdout_variables.get(key)
    }
    vs_paired = {
        key: {"other": _variables(paired).get(key), "holdout": holdout_variables.get(key)}
        for key in sorted(set(_variables(paired)) | set(holdout_variables))
        if _variables(paired).get(key) != holdout_variables.get(key)
    }
    return {
        "holdout_product_id": str(definition.product.id),
        "holdout_product_sha256": product_spec_hash(definition.product),
        "source_product_sha256": product_spec_hash(source),
        "paired_sibling_product_sha256": product_spec_hash(paired),
        "changed_product_variable_fields_vs_source": vs_source,
        "changed_product_variable_fields_vs_paired_sibling": vs_paired,
        "materially_different_vs_source": bool(vs_source)
        and definition.product.requested_outcome != source.requested_outcome,
        "materially_different_vs_paired_sibling": bool(vs_paired)
        and definition.product.requested_outcome != paired.requested_outcome,
        "requested_outcome_changed_vs_source": definition.product.requested_outcome != source.requested_outcome,
        "requested_outcome_changed_vs_paired_sibling": (
            definition.product.requested_outcome != paired.requested_outcome
        ),
        "class_invariants_equal": (
            definition.product.metadata["redi08_classification"]["class_invariants"]
            == source.metadata["redi08_classification"]["class_invariants"]
            == paired.metadata["redi08_classification"]["class_invariants"]
        ),
    }


# ---------------------------------------------------------------------------
# Recipe v2 revalidation decision + pinning
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Worker legitimacy classification (REDI-08C2)
# ---------------------------------------------------------------------------


def live_candidate_profiles() -> dict[str, dict[str, Any]]:
    """Approved REDI-07B live candidate profiles (provider-backed, same station)."""

    from .live_cold_start import DECLARED_LIVE_CANDIDATES

    implementation_station = str(controlled_warm_stations()[0].id)
    profiles: dict[str, dict[str, Any]] = {}
    for (policy_id, policy_version), candidate in DECLARED_LIVE_CANDIDATES.items():
        profiles[candidate.worker_id] = {
            "id": candidate.worker_id,
            "version": str(policy_version),
            "kind": "live_provider_candidate",
            "display_name": candidate.display_name,
            "provider": "openai",
            "model": candidate.model,
            "structured_output_capable": True,
            "station_eligibility": [implementation_station],
            "source_registry": "redi07b_declared_live_candidates",
            "declaration_policy": {"id": policy_id, "version": policy_version},
            "selection_basis": candidate.selection_basis,
        }
    return profiles


def gpt56_terra_holdout_worker_profile(version: int | None = None) -> dict[str, Any]:
    """The one REDI-08C6-authorized, purpose-bound genuine WorkerProfile.

    This registry entry is intentionally separate from the REDI-07B candidates:
    it is eligible only for the governed REDI-08C implementation station and
    records the exact non-secret provider configuration before any live
    result exists.

    Versions are immutable and reconstructible.  Version 1 is the byte-exact
    REDI-08C6 authorization (hard completion ceiling 1024, effort medium);
    version 2 (REDI-08C10) differs ONLY in ``max_output_tokens`` (2048);
    version 3 (REDI-08C12) differs from v2 ONLY in ``reasoning_effort`` (low);
    version 4 (REDI-08C14) differs from v3 ONLY in the execution-API strategy
    (Chat Completions -> Responses: api, capability profile, output parameter,
    usage shape, stateless single-call marker).  Every other field — provider,
    model, eligibility, structured-output capability, effort, ceiling,
    temperature/top_p behavior — is identical by construction.
    """

    resolved = int(version) if version is not None else GPT56_TERRA_WORKER_VERSION
    budgets = TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION
    efforts = TERRA_REASONING_EFFORT_BY_PROFILE_VERSION
    if resolved not in budgets or resolved not in efforts:
        raise HoldoutError(
            f"no frozen GPT-5.6 Terra WorkerProfile version {resolved}; "
            f"known versions: {sorted(budgets)}"
        )
    implementation_station = str(controlled_warm_stations()[0].id)
    api_fields = (
        {
            "api": "chat_completions",
            "api_capability_profile": "openai_chat_gpt56_v1",
            "max_output_parameter": "max_completion_tokens",
            "usage_shape": "chat_completions_usage_v1",
        }
        if resolved <= 3
        else {
            "api": "responses",
            "api_capability_profile": "openai_responses_gpt56_v1",
            "max_output_parameter": "max_output_tokens",
            "usage_shape": "responses_usage_v1",
            "conversation_state": "stateless_single_call",
        }
    )
    return {
        "id": GPT56_TERRA_WORKER_ID,
        "version": resolved,
        "kind": "provider_candidate",
        "display_name": "REDI-08C6 OpenAI GPT-5.6 Terra holdout worker",
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "purpose": "REDI-08C genuine holdout transfer only",
        "station_eligibility": [implementation_station],
        "task_eligibility": ["warm_start_component"],
        "structured_output_required": True,
        "actual_execution": True,
        "source_registry": "redi08c6_owner_authorized_worker_profile",
        "classification": WORKER_CLASS_GENUINE,
        "runtime": {
            "adapter": "LiveProviderAdapter",
            **api_fields,
            "response_format": "json_schema_strict",
            "max_output_tokens": budgets[resolved],
            "reasoning_effort": efforts[resolved],
            "temperature": None,
            "top_p": None,
            "provider_access_status": "provider_access_not_yet_proven",
        },
        "selection_basis": (
            "owner-authorized additional genuine candidate after two genuine "
            "workers failed the same frozen holdout; no quality, cost, or global "
            "preference claim"
        ),
    }


def extended_worker_registry() -> dict[str, dict[str, Any]]:
    """Pair registry plus declared live candidates and one scope-bound C6 profile."""

    registry = dict(worker_profiles())
    registry.update(live_candidate_profiles())
    terra = gpt56_terra_holdout_worker_profile()
    registry[str(terra["id"])] = terra
    return registry


def classify_registry_worker(worker_id: str) -> dict[str, Any]:
    """Deterministic legitimacy classification of one registry worker.

    Basis is the approved registry declaration plus the REDI-08C1 oracle
    finding; profile names are never used as evidence of legitimacy.
    """

    extended = extended_worker_registry()
    profile = extended.get(worker_id)
    if worker_id in live_candidate_profiles():
        live_profile = live_candidate_profiles()[worker_id]
        return {
            "worker_id": worker_id,
            "classification": WORKER_CLASS_GENUINE,
            "basis": (
                "provider-backed live candidate already declared in the REDI-07B "
                "exploration registry; its runner is the LiveProviderAdapter and its "
                "patch is generated from the compiled governed context"
            ),
            "kind": live_profile["kind"],
            "provider": live_profile["provider"],
            "model": live_profile["model"],
            "generates_output_from_context": True,
            "uses_reference_implementation": False,
            "fixture_specific_logic": False,
        }
    if profile is None:
        return {
            "worker_id": worker_id,
            "classification": WORKER_CLASS_INELIGIBLE,
            "basis": "not declared in the approved worker registries",
            "kind": None,
            "provider": None,
            "model": None,
            "generates_output_from_context": False,
            "uses_reference_implementation": False,
            "fixture_specific_logic": False,
        }
    kind = profile.get("kind")
    common = {
        "worker_id": worker_id,
        "kind": kind,
        "provider": profile.get("provider"),
        "model": profile.get("model"),
    }
    if profile.get("actual_execution") is False:
        return common | {
            "classification": WORKER_CLASS_INELIGIBLE,
            "basis": "profile is policy-only (actual_execution=false); it cannot execute work",
            "generates_output_from_context": False,
            "uses_reference_implementation": False,
            "fixture_specific_logic": False,
        }
    if kind == "provider_candidate":
        return common | {
            "classification": WORKER_CLASS_GENUINE,
            "basis": (
                "provider-backed structured-output worker; its runner is the "
                "LiveProviderAdapter and its patch is generated from the compiled "
                "governed context (ProductSpec/TaskContract/station/context/source "
                "files/public criteria), never from reference implementation bytes"
            ),
            "generates_output_from_context": True,
            "uses_reference_implementation": False,
            "fixture_specific_logic": False,
        }
    if kind == "local_deterministic":
        return common | {
            "classification": WORKER_CLASS_ORACLE,
            "basis": (
                "REDI-08C1 finding: its REDI-08C execution output is the "
                "predeclared_reference_implementation supplied by the fixture/harness, "
                "so it cannot produce genuine transfer acceptance evidence"
            ),
            "generates_output_from_context": False,
            "uses_reference_implementation": True,
            "fixture_specific_logic": True,
        }
    if kind == "local_independent_reviewer":
        return common | {
            "classification": WORKER_CLASS_TEST_ONLY,
            "basis": (
                "deterministic independent reviewer; allowed as verification "
                "machinery (strict-gate profile), never as implementation generation"
            ),
            "generates_output_from_context": False,
            "uses_reference_implementation": False,
            "fixture_specific_logic": False,
        }
    return common | {
        "classification": WORKER_CLASS_INELIGIBLE,
        "basis": f"unrecognised profile kind {kind!r}",
        "generates_output_from_context": False,
        "uses_reference_implementation": False,
        "fixture_specific_logic": False,
    }


def audit_worker_registry() -> tuple[dict[str, Any], ...]:
    """Deterministic classification of every approved worker (pair + live)."""

    worker_ids = list(WORKER_UNIVERSE) + sorted(live_candidate_profiles()) + [GPT56_TERRA_WORKER_ID]
    return tuple(classify_registry_worker(worker_id) for worker_id in worker_ids)


def load_genuine_holdout_failure_record(build_root: Path | str | None = None) -> dict[str, Any]:
    """Ground the genuine holdout failure in the persisted run result if present."""

    record = dict(GENUINE_HOLDOUT_FAILURE)
    root = Path(build_root) if build_root is not None else Path(__file__).resolve().parents[1] / "build"
    artifact = root / "redi08c-holdout-20260920T202934Z" / "run" / "holdout_result.json"
    if artifact.exists():
        try:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return record
        if (
            payload.get("factory_run_id") == GENUINE_HOLDOUT_FAILURE["factory_run_id"]
            and payload.get("verified") is False
            and payload.get("holdout_product_spec_sha256")
            == GENUINE_HOLDOUT_FAILURE["holdout_product_spec_sha256"]
        ):
            record["source"] = "persisted_artifact_verified"
    return record


def recipe_v2_fallback_audit(stored: StoredRecipeVersion) -> dict[str, Any]:
    """Audit the pinned recipe ladder for an eligible worker after the failed one."""

    implementation_station = str(controlled_warm_stations()[0].id)
    ladder = [
        str(worker)
        for worker in stored.recipe.worker_preferences.get(StationId(implementation_station), ())
    ]
    profiles = worker_profiles()
    candidates_after_failed: list[dict[str, Any]] = []
    seen_failed = False
    for worker_id in ladder:
        if worker_id == OWNER_PAIRED_002_FAILED_WORKER:
            seen_failed = True
            continue
        if not seen_failed:
            continue
        profile = profiles.get(worker_id, {})
        candidates_after_failed.append({
            "worker_id": worker_id,
            "configured": True,
            "station_eligible": implementation_station
            in [str(s) for s in profile.get("station_eligibility", ())],
            "actual_execution": bool(profile.get("actual_execution", True)),
            "kind": profile.get("kind"),
        })
    eligible = [
        item
        for item in candidates_after_failed
        if item["configured"] and item["station_eligible"] and item["actual_execution"]
    ]
    genuine_in_ladder = [
        worker_id
        for worker_id in ladder
        if classify_registry_worker(worker_id)["classification"] == WORKER_CLASS_GENUINE
    ]
    if stored.recipe.version == HISTORICAL_RECIPE_VERSION + 2:
        return {
            "policy_id": REVALIDATION_DECISION_POLICY_ID,
            "policy_version": REVALIDATION_DECISION_POLICY_VERSION,
            "recipe_id": str(stored.recipe.id),
            "recipe_version": stored.recipe.version,
            "implementation_station": implementation_station,
            "declared_ladder": ladder,
            "failed_workers": [
                GENUINE_HOLDOUT_FAILURE["failed_worker"],
                ALTERNATE_GENUINE_HOLDOUT_FAILURE["failed_worker"],
            ],
            "genuine_execution_workers_in_ladder": genuine_in_ladder,
            "decision": "V4_TERRA_WORKER_POLICY_REVALIDATION_PINNED",
            "new_recipe_version_created": False,
            "worker_policy_only_revision": True,
            "rationale": (
                "immutable v4 worker-policy revalidation is pinned to the single "
                "owner-authorized GPT-5.6 Terra worker; no fallback execution is allowed"
            ),
        }
    if stored.recipe.version == HISTORICAL_RECIPE_VERSION + 3:
        return {
            "policy_id": REVALIDATION_DECISION_POLICY_ID,
            "policy_version": REVALIDATION_DECISION_POLICY_VERSION,
            "recipe_id": str(stored.recipe.id),
            "recipe_version": stored.recipe.version,
            "implementation_station": implementation_station,
            "declared_ladder": ladder,
            "failed_workers": [
                GENUINE_HOLDOUT_FAILURE["failed_worker"],
                ALTERNATE_GENUINE_HOLDOUT_FAILURE["failed_worker"],
            ],
            "genuine_execution_workers_in_ladder": genuine_in_ladder,
            "decision": "V5_COMPLETION_BUDGET_REVALIDATION_PINNED",
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "authorized_worker_profile_version": 2,
            "authorized_completion_budget": {
                "parameter": "max_completion_tokens",
                "tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[2],
                "hard_ceiling_not_expected_usage": True,
            },
            "prior_completion_budget_tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[1],
            "reasoning_effort_unchanged": "medium",
            "new_recipe_version_created": True,
            "worker_policy_only_revision": True,
            "completion_budget_only_revision": True,
            "rationale": (
                "immutable v5 completion-budget revalidation keeps the same "
                "owner-authorized GPT-5.6 Terra worker and only raises the hard "
                "completion ceiling 1024 -> 2048 after the recorded "
                "finish_reason=length truncation; no fallback execution is allowed "
                "and no worker capability claim is made"
            ),
        }
    if stored.recipe.version == HISTORICAL_RECIPE_VERSION + 4:
        return {
            "policy_id": REVALIDATION_DECISION_POLICY_ID,
            "policy_version": REVALIDATION_DECISION_POLICY_VERSION,
            "recipe_id": str(stored.recipe.id),
            "recipe_version": stored.recipe.version,
            "implementation_station": implementation_station,
            "declared_ladder": ladder,
            "failed_workers": [
                GENUINE_HOLDOUT_FAILURE["failed_worker"],
                ALTERNATE_GENUINE_HOLDOUT_FAILURE["failed_worker"],
            ],
            "genuine_execution_workers_in_ladder": genuine_in_ladder,
            "decision": "V6_REASONING_REVALIDATION_PINNED",
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "authorized_worker_profile_version": 3,
            "authorized_reasoning_effort": TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[3],
            "prior_reasoning_effort": TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[2],
            "authorized_completion_budget": {
                "parameter": "max_completion_tokens",
                "tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[3],
                "hard_ceiling_not_expected_usage": True,
            },
            "completion_budget_unchanged": True,
            "new_recipe_version_created": True,
            "worker_policy_only_revision": True,
            "reasoning_config_only_revision": True,
            "rationale": (
                "immutable v6 reasoning revalidation keeps the same owner-"
                "authorized GPT-5.6 Terra worker, model, and 2048 hard completion "
                "ceiling, and only lowers reasoning_effort medium -> low after TWO "
                "recorded finish_reason=length full-reasoning saturations "
                "(1024/1024 and 2048/2048, zero visible answer); no fallback "
                "execution is allowed and this is not a worker capability "
                "downgrade or upgrade claim"
            ),
        }
    if stored.recipe.version == HISTORICAL_RECIPE_VERSION + 5:
        return {
            "policy_id": REVALIDATION_DECISION_POLICY_ID,
            "policy_version": REVALIDATION_DECISION_POLICY_VERSION,
            "recipe_id": str(stored.recipe.id),
            "recipe_version": stored.recipe.version,
            "implementation_station": implementation_station,
            "declared_ladder": ladder,
            "failed_workers": [
                GENUINE_HOLDOUT_FAILURE["failed_worker"],
                ALTERNATE_GENUINE_HOLDOUT_FAILURE["failed_worker"],
            ],
            "genuine_execution_workers_in_ladder": genuine_in_ladder,
            "decision": "V7_RESPONSES_STRATEGY_REVALIDATION_PINNED",
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "authorized_worker_profile_version": 4,
            "authorized_reasoning_effort": TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[4],
            "reasoning_effort_unchanged": "low",
            "authorized_completion_budget": {
                "parameter": "max_output_tokens",
                "tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[4],
                "hard_ceiling_not_expected_usage": True,
            },
            "completion_budget_unchanged": True,
            "authorized_execution_api": {
                "prior": "chat_completions",
                "value": "responses",
                "api_capability_profile": "openai_responses_gpt56_v1",
            },
            "conversation_state": "stateless_single_call",
            "new_recipe_version_created": True,
            "worker_policy_only_revision": True,
            "execution_api_only_revision": True,
            "rationale": (
                "immutable v7 execution-strategy revalidation keeps the same "
                "owner-authorized GPT-5.6 Terra worker, model, low reasoning "
                "effort, and 2048 hard completion ceiling, and only moves the "
                "execution boundary chat_completions -> Responses API after "
                "THREE recorded finish_reason=length full-reasoning saturations "
                "(medium/1024, medium/2048, low/2048, zero visible answer); no "
                "fallback execution is allowed, no model-capability conclusion "
                "is drawn, and no Responses superiority claim is made before "
                "the live experiment"
            ),
        }
    if stored.recipe.version == HISTORICAL_RECIPE_VERSION + 1:
        return {
            "policy_id": REVALIDATION_DECISION_POLICY_ID,
            "policy_version": REVALIDATION_DECISION_POLICY_VERSION,
            "recipe_id": str(stored.recipe.id),
            "recipe_version": stored.recipe.version,
            "implementation_station": implementation_station,
            "declared_ladder": ladder,
            "failed_worker": OWNER_PAIRED_002_FAILED_WORKER,
            "genuine_execution_workers_in_ladder": genuine_in_ladder,
            "decision": "V3_WORKER_POLICY_REVALIDATION_PINNED",
            "new_recipe_version_created": False,
            "worker_policy_only_revision": True,
            "rationale": (
                "immutable v3 worker-policy revalidation pinned; oracle/test tail "
                "workers remain barred from canonical transfer acceptance"
            ),
        }
    return {
        "policy_id": REVALIDATION_DECISION_POLICY_ID,
        "policy_version": REVALIDATION_DECISION_POLICY_VERSION,
        "recipe_id": str(stored.recipe.id),
        "recipe_version": stored.recipe.version,
        "implementation_station": implementation_station,
        "declared_ladder": ladder,
        "failed_worker": OWNER_PAIRED_002_FAILED_WORKER,
        "candidates_after_failed_worker": candidates_after_failed,
        "another_eligible_worker_exists": bool(eligible),
        "genuine_execution_workers_in_ladder": genuine_in_ladder,
        "genuine_worker_is_recipe_preferred": bool(genuine_in_ladder) and genuine_in_ladder[0] == ladder[0],
        "canonical_transfer_decision": (
            "USE_V2_AS_IS_PREFERRED_WORKER_IS_GENUINE"
            if genuine_in_ladder and genuine_in_ladder[0] == ladder[0]
            else "RECIPE_V3_REVALIDATION_REQUIRED"
        ),
        "decision": "REUSE_RECIPE_V2_FALLBACK_LADDER" if eligible else "RECIPE_V3_REVALIDATION_REQUIRED",
        "new_recipe_version_created": False,
        "rationale": (
            "the existing immutable recipe v2 already declares the genuine execution "
            "worker as its preferred implementation worker; oracle/test tail workers "
            "are excluded from canonical transfer acceptance, so no v3 is created"
            if genuine_in_ladder and genuine_in_ladder[0] == ladder[0]
            else (
                "the existing immutable recipe v2 fallback ladder already declares another "
                "configured, station-eligible, executing worker after the failed preferred worker"
                if eligible
                else "no eligible executing worker remains after the failed preferred worker"
            )
        ),
    }


def create_alternate_worker_v3(
    *,
    store: RecipeStore,
    stored_v2: StoredRecipeVersion,
    product_class: ProductClass,
    owner_paired_002_record: Mapping[str, Any],
    genuine_failure_record: Mapping[str, Any],
    alternate_worker_id: str,
    retained_fallback_worker_id: str = OWNER_PAIRED_002_FAILED_WORKER,
) -> StoredRecipeVersion:
    """Register immutable recipe v3: worker preference/fallback policy only.

    Everything else (ProductClass, topology, stations, task decomposition,
    context strategy/budget, gate/retry/adaptation/integration/parallelization
    policies) is byte-identical to v2; only the implementation-station worker
    ladder changes, promoting the alternate genuine worker to preference 1 and
    keeping the already-failed gpt-4.1 worker as declared (never automatic)
    fallback.
    """

    if stored_v2.recipe.version != HISTORICAL_RECIPE_VERSION:
        raise HoldoutError("alternate-worker revalidation must start from recipe v2")
    implementation_station = str(controlled_warm_stations()[0].id)
    review_station = str(controlled_warm_stations()[1].id)
    new_preferences = dict(stored_v2.recipe.worker_preferences)
    new_preferences[StationId(implementation_station)] = (
        WorkerId(alternate_worker_id),
        WorkerId(retained_fallback_worker_id),
    )
    new_fallbacks = dict(stored_v2.recipe.worker_fallbacks)
    new_fallbacks[StationId(implementation_station)] = (
        WorkerId(alternate_worker_id),
        WorkerId(retained_fallback_worker_id),
    )
    from dataclasses import replace
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    v3_recipe = replace(
        stored_v2.recipe,
        version=HISTORICAL_RECIPE_VERSION + 1,
        worker_preferences=new_preferences,
        worker_fallbacks=new_fallbacks,
        created_at=now,
        last_revalidated_at=now,
    )
    provenance = dict(stored_v2.provenance) | {
        "parent_recipe_version": HISTORICAL_RECIPE_VERSION,
        "parent_recipe_semantic_hash": canonical_recipe_semantic_hash(stored_v2.recipe),
        "revalidation_policy_id": ALTERNATE_WORKER_REVALIDATION_POLICY_ID,
        "revalidation_policy_version": ALTERNATE_WORKER_REVALIDATION_POLICY_VERSION,
        "revalidation_evidence": {
            "owner_paired_002": {
                "pair_id": owner_paired_002_record["pair_id"],
                "classification": owner_paired_002_record["classification"],
                "failure_class": owner_paired_002_record["failure_class"],
                "canonical_hash": owner_paired_002_record["canonical_hash"],
            },
            "genuine_holdout_failure": {
                "factory_run_id": genuine_failure_record["factory_run_id"],
                "failed_worker": genuine_failure_record["failed_worker"],
                "model": genuine_failure_record["model"],
                "failed_boundary": genuine_failure_record["failed_boundary"],
                "failure_class": genuine_failure_record["failure_class"],
                "failure_record_id": genuine_failure_record["failure_record_id"],
                "failure_fingerprint": genuine_failure_record["failure_fingerprint"],
                "holdout_product_spec_sha256": genuine_failure_record[
                    "holdout_product_spec_sha256"
                ],
                "source": genuine_failure_record["source"],
            },
            "worker_policy_revision_reason": (
                "REDI-08C2/C3: oracle and test workers are barred from canonical "
                "transfer evidence, and the previously preferred genuine worker "
                "already failed this exact holdout, so the next attempt must change "
                "worker identity; the alternate declared live candidate is promoted "
                "to preference 1 while the failed worker stays a declared (never "
                "automatic) fallback"
            ),
            "oracle_test_workers_barred": True,
        },
    }
    stored_v3 = store.register_version(
        recipe=v3_recipe,
        product_class=product_class,
        confidence=stored_v2.confidence,
        provenance=provenance,
        parent_version=HISTORICAL_RECIPE_VERSION,
        rollback_target_version=HISTORICAL_RECIPE_VERSION,
        revalidation_state="VALID_FOR_CONTROLLED_EXPERIMENT",
        registration_metadata={
            "revision_kind": "worker_policy_only",
            "alternate_worker_id": alternate_worker_id,
            "holdout_frozen": True,
        },
        event_type="REVALIDATION_VERSION_CREATED",
        policy_id=ALTERNATE_WORKER_REVALIDATION_POLICY_ID,
        policy_version=ALTERNATE_WORKER_REVALIDATION_POLICY_VERSION,
        event_evidence={
            "parent_version": HISTORICAL_RECIPE_VERSION,
            "alternate_worker_id": alternate_worker_id,
            "genuine_holdout_failure_factory_run_id": genuine_failure_record[
                "factory_run_id"
            ],
            "owner_paired_002_pair_id": owner_paired_002_record["pair_id"],
        },
        event_reason="worker-policy-only SHADOW revalidation for an alternate genuine worker",
    )
    if stored_v3.lifecycle_state is not RecipeRung.SHADOW:
        raise HoldoutError("recipe v3 must remain SHADOW")
    return stored_v3


def create_gpt56_terra_worker_v4(
    *,
    store: RecipeStore,
    stored_v3: StoredRecipeVersion,
    product_class: ProductClass,
    gpt41_failure_record: Mapping[str, Any],
    gpt4o_mini_failure_record: Mapping[str, Any],
) -> StoredRecipeVersion:
    """Register the C6 SHADOW v4 with Terra as the one execution slot.

    The v4 delta is intentionally limited to the implementation worker
    preference/fallback tuple.  The tuple contains Terra only, mechanically
    prohibiting a second worker or fallback after the owner uses its one live
    slot.  All product, topology, station, task, context, budget, gate,
    integration, reviewer, and parallelization fields remain inherited from v3.
    """

    expected_v3 = HISTORICAL_RECIPE_VERSION + 1
    if stored_v3.recipe.version != expected_v3:
        raise HoldoutError("GPT-5.6 Terra revalidation must start from recipe v3")
    terra = gpt56_terra_holdout_worker_profile(version=1)
    if terra["classification"] != WORKER_CLASS_GENUINE or not terra["actual_execution"]:
        raise HoldoutError("GPT-5.6 Terra WorkerProfile is not a genuine executing worker")
    if terra["runtime"]["reasoning_effort"] != "medium":
        raise HoldoutError("GPT-5.6 Terra reasoning effort must be preregistered as medium")
    if terra["runtime"]["max_output_tokens"] != TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[1]:
        raise HoldoutError("v4 authorizes exactly the frozen 1024 completion budget")
    if gpt41_failure_record.get("model") != GENUINE_HOLDOUT_FAILURE["model"]:
        raise HoldoutError("v4 provenance requires the recorded GPT-4.1 failure")
    if gpt4o_mini_failure_record.get("model") != ALTERNATE_GENUINE_HOLDOUT_FAILURE["model"]:
        raise HoldoutError("v4 provenance requires the recorded GPT-4o-mini failure")

    implementation_station = StationId(str(controlled_warm_stations()[0].id))
    new_preferences = dict(stored_v3.recipe.worker_preferences)
    new_fallbacks = dict(stored_v3.recipe.worker_fallbacks)
    terra_only = (WorkerId(GPT56_TERRA_WORKER_ID),)
    new_preferences[implementation_station] = terra_only
    new_fallbacks[implementation_station] = terra_only

    from dataclasses import replace
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    v4_recipe = replace(
        stored_v3.recipe,
        version=expected_v3 + 1,
        worker_preferences=new_preferences,
        worker_fallbacks=new_fallbacks,
        created_at=now,
        last_revalidated_at=now,
    )
    parent_semantic_hash = canonical_recipe_semantic_hash(stored_v3.recipe)
    provenance = dict(stored_v3.provenance) | {
        "parent_recipe_version": expected_v3,
        "parent_recipe_semantic_hash": parent_semantic_hash,
        "lineage_v2_recipe_semantic_hash": HISTORICAL_RECIPE_SEMANTIC_HASH,
        "revalidation_policy_id": GPT56_TERRA_REVALIDATION_POLICY_ID,
        "revalidation_policy_version": GPT56_TERRA_REVALIDATION_POLICY_VERSION,
        "revalidation_evidence": {
            "parent_v3": {
                "recipe_id": str(stored_v3.recipe.id),
                "recipe_version": expected_v3,
                "recipe_semantic_hash": parent_semantic_hash,
                "lifecycle_state": stored_v3.lifecycle_state.value,
            },
            "gpt41_genuine_failure": dict(gpt41_failure_record),
            "gpt4o_mini_genuine_failure": dict(gpt4o_mini_failure_record),
            "cross_worker_audit": REPEATED_WORKER_FAILURE_AUDIT,
            "owner_authorization": {
                "authorization": "REDI-08C6",
                "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
                "purpose": "REDI-08C genuine holdout transfer only",
                "paid_live_provider_call_authorized": False,
            },
            "worker_policy_revision_reason": (
                "Two distinct genuine workers recorded the same public semantic "
                "defect on the frozen holdout while recipe configuration was not "
                "causal; the owner authorized one new genuine GPT-5.6 Terra "
                "candidate for additional transfer evidence. This is not a claim "
                "that the new candidate is better."
            ),
            "automatic_paid_retry": False,
            "fallback_after_terra_forbidden": True,
        },
    }
    stored_v4 = store.register_version(
        recipe=v4_recipe,
        product_class=product_class,
        confidence=stored_v3.confidence,
        provenance=provenance,
        parent_version=expected_v3,
        rollback_target_version=expected_v3,
        revalidation_state="VALID_FOR_CONTROLLED_EXPERIMENT",
        registration_metadata={
            "revision_kind": "worker_policy_only",
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "holdout_frozen": True,
            "automatic_paid_retry": False,
        },
        event_type="REVALIDATION_VERSION_CREATED",
        policy_id=GPT56_TERRA_REVALIDATION_POLICY_ID,
        policy_version=GPT56_TERRA_REVALIDATION_POLICY_VERSION,
        event_evidence={
            "parent_version": expected_v3,
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "gpt41_failure_factory_run_id": gpt41_failure_record["factory_run_id"],
            "gpt4o_mini_failure_factory_run_id": gpt4o_mini_failure_record["factory_run_id"],
            "cross_worker_audit": REPEATED_WORKER_FAILURE_AUDIT,
        },
        event_reason="worker-policy-only SHADOW revalidation for owner-authorized GPT-5.6 Terra",
    )
    if stored_v4.lifecycle_state is not RecipeRung.SHADOW:
        raise HoldoutError("recipe v4 must remain SHADOW")
    return stored_v4


def create_completion_budget_revalidation_v5(
    *,
    store: RecipeStore,
    stored_v4: StoredRecipeVersion,
    product_class: ProductClass,
) -> StoredRecipeVersion:
    """Register the C10 SHADOW v5: the completion-budget-only revalidation.

    The v5 delta is intentionally limited to the worker/runtime completion-
    budget reference (hard ceiling 1024 -> 2048 on the SAME owner-authorized
    Terra WorkerProfile, now version 2).  Provider, model, eligibility,
    reasoning effort, structured-output capability, topology, stations, tasks,
    context semantics, gate policy, and the builder output contract remain
    inherited from v4 unchanged.  Provenance records the C9-audited truncated
    run; no Terra capability claim is expressed or implied.  The version is
    born SHADOW and is never promoted here.
    """

    expected_v4 = HISTORICAL_RECIPE_VERSION + 2
    if stored_v4.recipe.version != expected_v4:
        raise HoldoutError("completion-budget revalidation must start from recipe v4")
    terra = gpt56_terra_holdout_worker_profile(version=2)
    if terra["classification"] != WORKER_CLASS_GENUINE or not terra["actual_execution"]:
        raise HoldoutError("completion-budget revalidation requires the genuine Terra worker")
    if terra["runtime"]["reasoning_effort"] != "medium":
        raise HoldoutError("completion-budget revalidation must keep reasoning_effort=medium")
    if terra["runtime"]["max_output_tokens"] != TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[2]:
        raise HoldoutError("v5 must bind exactly the 2048 hard completion ceiling")
    if HISTORICAL_TERRA_TRUNCATED_RUN["finish_reason"] != "length" or not (
        HISTORICAL_TERRA_TRUNCATED_RUN["incomplete"]
    ):
        raise HoldoutError("v5 provenance requires the recorded finish_reason=length truncation")
    if (
        HISTORICAL_TERRA_TRUNCATED_RUN["output_tokens"] != 1024
        or HISTORICAL_TERRA_TRUNCATED_RUN["reasoning_tokens"] != 1024
    ):
        raise HoldoutError("v5 provenance requires the recorded 1024/1024 reasoning exhaustion")

    from dataclasses import replace
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    budget_metadata = {
        "completion_budget_revalidation": {
            "policy": "REDI-08C10",
            "parameter": "max_completion_tokens",
            "prior_tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[1],
            "tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[2],
            "hard_ceiling_not_expected_usage": True,
            "worker_profile_id": GPT56_TERRA_WORKER_ID,
            "worker_profile_version": 2,
            "reasoning_effort_unchanged": "medium",
        }
    }
    v5_recipe = replace(
        stored_v4.recipe,
        version=expected_v4 + 1,
        metadata=dict(stored_v4.recipe.metadata) | budget_metadata,
        created_at=now,
        last_revalidated_at=now,
    )
    parent_semantic_hash = canonical_recipe_semantic_hash(stored_v4.recipe)
    provenance = dict(stored_v4.provenance) | {
        "parent_recipe_version": expected_v4,
        "parent_recipe_semantic_hash": parent_semantic_hash,
        "lineage_v2_recipe_semantic_hash": HISTORICAL_RECIPE_SEMANTIC_HASH,
        "revalidation_policy_id": COMPLETION_BUDGET_REVALIDATION_POLICY_ID,
        "revalidation_policy_version": COMPLETION_BUDGET_REVALIDATION_POLICY_VERSION,
        "revalidation_evidence": {
            "parent_v4": {
                "recipe_id": str(stored_v4.recipe.id),
                "recipe_version": expected_v4,
                "recipe_semantic_hash": parent_semantic_hash,
                "lifecycle_state": stored_v4.lifecycle_state.value,
            },
            "historical_terra_truncated_run": dict(HISTORICAL_TERRA_TRUNCATED_RUN),
            "budget_audit_verdict": HISTORICAL_TERRA_TRUNCATED_RUN["budget_audit_verdict"],
            "no_capability_claim": (
                "The budget change only raises the hard completion ceiling after a "
                "recorded finish_reason=length truncation.  It does not claim the "
                "Terra worker is better or worse than any other worker."
            ),
            "automatic_paid_retry": False,
            "fallback_after_terra_forbidden": True,
        },
    }
    stored_v5 = store.register_version(
        recipe=v5_recipe,
        product_class=product_class,
        confidence=stored_v4.confidence,
        provenance=provenance,
        parent_version=expected_v4,
        rollback_target_version=expected_v4,
        revalidation_state="VALID_FOR_CONTROLLED_EXPERIMENT",
        registration_metadata={
            "revision_kind": "completion_budget_only",
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "authorized_worker_profile_version": 2,
            "authorized_completion_budget": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[2],
            "holdout_frozen": True,
            "automatic_paid_retry": False,
        },
        event_type="REVALIDATION_VERSION_CREATED",
        policy_id=COMPLETION_BUDGET_REVALIDATION_POLICY_ID,
        policy_version=COMPLETION_BUDGET_REVALIDATION_POLICY_VERSION,
        event_evidence={
            "parent_version": expected_v4,
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "authorized_worker_profile_version": 2,
            "prior_completion_budget_tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[1],
            "completion_budget_tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[2],
            "historical_truncated_factory_run_id": (
                HISTORICAL_TERRA_TRUNCATED_RUN["factory_run_id"]
            ),
            "finish_reason": HISTORICAL_TERRA_TRUNCATED_RUN["finish_reason"],
        },
        event_reason=(
            "completion-budget-only SHADOW revalidation: Terra hard completion "
            "ceiling 1024 -> 2048 after the recorded finish_reason=length truncation"
        ),
    )
    if stored_v5.lifecycle_state is not RecipeRung.SHADOW:
        raise HoldoutError("recipe v5 must remain SHADOW")
    return stored_v5


def create_reasoning_revalidation_v6(
    *,
    store: RecipeStore,
    stored_v5: StoredRecipeVersion,
    product_class: ProductClass,
) -> StoredRecipeVersion:
    """Register the C12 SHADOW v6: the reasoning-configuration-only revalidation.

    The v6 delta is intentionally limited to the worker/runtime reasoning
    binding (reasoning_effort medium -> low on the SAME owner-authorized Terra
    WorkerProfile, now version 3, at the SAME 2048 hard completion ceiling).
    Provider, model, eligibility, structured-output capability, topology,
    stations, tasks, context semantics, gate policy, and the builder output
    contract remain inherited from v5 unchanged.  Provenance records BOTH
    medium-effort saturation runs and the C11 audit verdict; this is NOT a
    worker capability downgrade/upgrade claim.  The version is born SHADOW and
    is never promoted here.
    """

    expected_v5 = HISTORICAL_RECIPE_VERSION + 3
    if stored_v5.recipe.version != expected_v5:
        raise HoldoutError("reasoning revalidation must start from recipe v5")
    terra = gpt56_terra_holdout_worker_profile(version=3)
    if terra["classification"] != WORKER_CLASS_GENUINE or not terra["actual_execution"]:
        raise HoldoutError("reasoning revalidation requires the genuine Terra worker")
    if terra["runtime"]["reasoning_effort"] != TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[3]:
        raise HoldoutError("v6 must bind exactly the low reasoning effort")
    if terra["runtime"]["max_output_tokens"] != TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[3]:
        raise HoldoutError("v6 must keep the frozen 2048 completion ceiling")
    saturation = HISTORICAL_TERRA_SATURATION_RUNS
    for key in ("run_a_medium_1024", "run_b_medium_2048"):
        record = saturation[key]
        if record["finish_reason"] != "length" or not record["incomplete"]:
            raise HoldoutError("v6 provenance requires both finish_reason=length runs")
        if record["reasoning_tokens"] != record["output_tokens"]:
            raise HoldoutError("v6 provenance requires full reasoning saturation")
        if record["visible_answer_content"] != "none":
            raise HoldoutError("v6 provenance requires zero visible answer")
    if saturation["audit_verdict"] != "REASONING_BUDGET_SATURATION_REPEATED":
        raise HoldoutError("v6 provenance requires the repeated-saturation audit verdict")

    from dataclasses import replace
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    v6_recipe = replace(
        stored_v5.recipe,
        version=expected_v5 + 1,
        metadata=dict(stored_v5.recipe.metadata)
        | {"reasoning_revalidation": dict(REASONING_REVALIDATION_METADATA)},
        created_at=now,
        last_revalidated_at=now,
    )
    parent_semantic_hash = canonical_recipe_semantic_hash(stored_v5.recipe)
    provenance = dict(stored_v5.provenance) | {
        "parent_recipe_version": expected_v5,
        "parent_recipe_semantic_hash": parent_semantic_hash,
        "lineage_v2_recipe_semantic_hash": HISTORICAL_RECIPE_SEMANTIC_HASH,
        "revalidation_policy_id": REASONING_REVALIDATION_POLICY_ID,
        "revalidation_policy_version": REASONING_REVALIDATION_POLICY_VERSION,
        "revalidation_evidence": {
            "parent_v5": {
                "recipe_id": str(stored_v5.recipe.id),
                "recipe_version": expected_v5,
                "recipe_semantic_hash": parent_semantic_hash,
                "lifecycle_state": stored_v5.lifecycle_state.value,
            },
            "historical_terra_saturation_runs": json.loads(json.dumps(saturation)),
            "budget_audit_verdict": saturation["audit_verdict"],
            "no_capability_claim": saturation["capability_claim_boundary"],
            "automatic_paid_retry": False,
            "fallback_after_terra_forbidden": True,
        },
    }
    stored_v6 = store.register_version(
        recipe=v6_recipe,
        product_class=product_class,
        confidence=stored_v5.confidence,
        provenance=provenance,
        parent_version=expected_v5,
        rollback_target_version=expected_v5,
        revalidation_state="VALID_FOR_CONTROLLED_EXPERIMENT",
        registration_metadata={
            "revision_kind": "reasoning_config_only",
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "authorized_worker_profile_version": 3,
            "authorized_reasoning_effort": TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[3],
            "authorized_completion_budget": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[3],
            "holdout_frozen": True,
            "automatic_paid_retry": False,
        },
        event_type="REVALIDATION_VERSION_CREATED",
        policy_id=REASONING_REVALIDATION_POLICY_ID,
        policy_version=REASONING_REVALIDATION_POLICY_VERSION,
        event_evidence={
            "parent_version": expected_v5,
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "authorized_worker_profile_version": 3,
            "prior_reasoning_effort": TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[2],
            "reasoning_effort": TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[3],
            "completion_budget_tokens_unchanged": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[3],
            "medium_saturation_factory_run_ids": [
                saturation["run_a_medium_1024"]["factory_run_id"],
                saturation["run_b_medium_2048"]["factory_run_id"],
            ],
        },
        event_reason=(
            "reasoning-config-only SHADOW revalidation: Terra reasoning_effort "
            "medium -> low at the same 2048 ceiling after two recorded "
            "finish_reason=length saturations"
        ),
    )
    if stored_v6.lifecycle_state is not RecipeRung.SHADOW:
        raise HoldoutError("recipe v6 must remain SHADOW")
    return stored_v6


def create_responses_strategy_revalidation_v7(
    *,
    store: RecipeStore,
    stored_v6: StoredRecipeVersion,
    product_class: ProductClass,
) -> StoredRecipeVersion:
    """Register the C14 SHADOW v7: the execution-API-strategy-only revalidation.

    The v7 delta is intentionally limited to the worker/runtime execution-API
    strategy binding (Chat Completions -> Responses API on the SAME
    owner-authorized Terra WorkerProfile, now version 4, at the SAME low
    reasoning effort and the SAME 2048 hard completion ceiling).  Provider,
    model, eligibility, structured-output capability, topology, stations,
    tasks, context semantics, gate policy, and the builder output contract
    remain inherited from v6 unchanged.  Provenance records ALL THREE Chat
    Completions saturation runs (medium/1024, medium/2048, low/2048) and the
    C13 audit verdict EXECUTION_STRATEGY_REVIEW_REQUIRED; this is NOT a model
    capability conclusion and no Responses superiority claim is made before
    the live experiment.  The version is born SHADOW and is never promoted
    here.
    """

    expected_v6 = HISTORICAL_RECIPE_VERSION + 4
    if stored_v6.recipe.version != expected_v6:
        raise HoldoutError("responses-strategy revalidation must start from recipe v6")
    terra = gpt56_terra_holdout_worker_profile(version=4)
    if terra["classification"] != WORKER_CLASS_GENUINE or not terra["actual_execution"]:
        raise HoldoutError("responses-strategy revalidation requires the genuine Terra worker")
    if terra["runtime"]["reasoning_effort"] != TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[4]:
        raise HoldoutError("v7 must keep exactly the low reasoning effort")
    if terra["runtime"]["max_output_tokens"] != TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[4]:
        raise HoldoutError("v7 must keep the frozen 2048 completion ceiling")
    if terra["runtime"]["api"] != "responses":
        raise HoldoutError("v7 must bind exactly the responses execution API")
    if terra["runtime"]["api_capability_profile"] != RESPONSES_STRATEGY_REVALIDATION_METADATA["api_capability_profile"]:
        raise HoldoutError("v7 must bind the Responses capability profile")
    low_run = HISTORICAL_TERRA_LOW_SATURATION_RUN
    if low_run["finish_reason"] != "length" or not low_run["incomplete"]:
        raise HoldoutError("v7 provenance requires the finish_reason=length low run")
    if low_run["reasoning_tokens"] != low_run["output_tokens"] or low_run["visible_answer_content"] != "none":
        raise HoldoutError("v7 provenance requires full low-effort reasoning saturation")
    if HISTORICAL_TERRA_EXECUTION_STRATEGY_AUDIT["audit_verdict"] != "EXECUTION_STRATEGY_REVIEW_REQUIRED":
        raise HoldoutError("v7 provenance requires the execution-strategy audit verdict")

    from dataclasses import replace
    from datetime import datetime, timezone as _tz

    now = datetime.now(_tz.utc)
    v7_recipe = replace(
        stored_v6.recipe,
        version=expected_v6 + 1,
        metadata=dict(stored_v6.recipe.metadata)
        | {"responses_strategy_revalidation": dict(RESPONSES_STRATEGY_REVALIDATION_METADATA)},
        created_at=now,
        last_revalidated_at=now,
    )
    parent_semantic_hash = canonical_recipe_semantic_hash(stored_v6.recipe)
    provenance = dict(stored_v6.provenance) | {
        "parent_recipe_version": expected_v6,
        "parent_recipe_semantic_hash": parent_semantic_hash,
        "lineage_v2_recipe_semantic_hash": HISTORICAL_RECIPE_SEMANTIC_HASH,
        "revalidation_policy_id": EXECUTION_STRATEGY_REVALIDATION_POLICY_ID,
        "revalidation_policy_version": EXECUTION_STRATEGY_REVALIDATION_POLICY_VERSION,
        "revalidation_evidence": {
            "parent_v6": {
                "recipe_id": str(stored_v6.recipe.id),
                "recipe_version": expected_v6,
                "recipe_semantic_hash": parent_semantic_hash,
                "lifecycle_state": stored_v6.lifecycle_state.value,
            },
            "historical_terra_chat_completions_runs": {
                "run_a_medium_1024": json.loads(
                    json.dumps(HISTORICAL_TERRA_SATURATION_RUNS["run_a_medium_1024"])
                ),
                "run_b_medium_2048": json.loads(
                    json.dumps(HISTORICAL_TERRA_SATURATION_RUNS["run_b_medium_2048"])
                ),
                "run_c_low_2048": dict(HISTORICAL_TERRA_LOW_SATURATION_RUN),
            },
            "execution_strategy_audit": dict(HISTORICAL_TERRA_EXECUTION_STRATEGY_AUDIT),
            "no_capability_claim": HISTORICAL_TERRA_EXECUTION_STRATEGY_AUDIT["capability_claim_boundary"],
            "automatic_paid_retry": False,
            "fallback_after_terra_forbidden": True,
        },
    }
    stored_v7 = store.register_version(
        recipe=v7_recipe,
        product_class=product_class,
        confidence=stored_v6.confidence,
        provenance=provenance,
        parent_version=expected_v6,
        rollback_target_version=expected_v6,
        revalidation_state="VALID_FOR_CONTROLLED_EXPERIMENT",
        registration_metadata={
            "revision_kind": "execution_api_only",
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "authorized_worker_profile_version": 4,
            "authorized_reasoning_effort": TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[4],
            "authorized_completion_budget": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[4],
            "authorized_execution_api": RESPONSES_STRATEGY_REVALIDATION_METADATA["value"],
            "prior_execution_api": RESPONSES_STRATEGY_REVALIDATION_METADATA["prior_value"],
            "holdout_frozen": True,
            "automatic_paid_retry": False,
        },
        event_type="REVALIDATION_VERSION_CREATED",
        policy_id=EXECUTION_STRATEGY_REVALIDATION_POLICY_ID,
        policy_version=EXECUTION_STRATEGY_REVALIDATION_POLICY_VERSION,
        event_evidence={
            "parent_version": expected_v6,
            "authorized_worker_profile": GPT56_TERRA_WORKER_ID,
            "authorized_worker_profile_version": 4,
            "prior_execution_api": RESPONSES_STRATEGY_REVALIDATION_METADATA["prior_value"],
            "execution_api": RESPONSES_STRATEGY_REVALIDATION_METADATA["value"],
            "reasoning_effort_unchanged": TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[4],
            "completion_budget_tokens_unchanged": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[4],
            "chat_completions_saturation_factory_run_ids": [
                HISTORICAL_TERRA_SATURATION_RUNS["run_a_medium_1024"]["factory_run_id"],
                HISTORICAL_TERRA_SATURATION_RUNS["run_b_medium_2048"]["factory_run_id"],
                HISTORICAL_TERRA_LOW_SATURATION_RUN["factory_run_id"],
            ],
        },
        event_reason=(
            "execution-api-only SHADOW revalidation: Terra execution boundary "
            "chat_completions -> Responses API at the same low effort and 2048 "
            "ceiling after three recorded finish_reason=length saturations"
        ),
    )
    if stored_v7.lifecycle_state is not RecipeRung.SHADOW:
        raise HoldoutError("recipe v7 must remain SHADOW")
    return stored_v7


def prepare_holdout_recipe(
    *,
    store: RecipeStore,
    source_ledger: Path | str,
    source_factory_run_id: str,
    product_class: ProductClass,
    expected_semantic_hash: str | None = HISTORICAL_RECIPE_SEMANTIC_HASH,
    approval_reference: str = "redi08c-holdout-revalidation",
) -> StoredRecipeVersion:
    """Reproduce and pin the immutable SHADOW recipe v2 (never a new version)."""

    stored = prepare_shadow_recipe(
        store=store,
        source_ledger=source_ledger,
        source_factory_run_id=source_factory_run_id,
        product_class=product_class,
        approval_reference=approval_reference,
    )
    if str(stored.recipe.id) != HISTORICAL_RECIPE_ID or stored.recipe.version != HISTORICAL_RECIPE_VERSION:
        raise HoldoutError("reproduced recipe identity is not the accepted recipe v2")
    if stored.lifecycle_state is not RecipeRung.SHADOW:
        raise HoldoutError("holdout requires the SHADOW recipe v2")
    semantic_hash = canonical_recipe_semantic_hash(stored.recipe)
    if expected_semantic_hash is not None and semantic_hash != expected_semantic_hash:
        raise HoldoutError(
            "reproduced recipe v2 semantic hash does not match the owner-paired-002 recipe identity"
        )
    return stored


# ---------------------------------------------------------------------------
# Recipe-ladder worker selection
# ---------------------------------------------------------------------------


def select_holdout_worker(
    *,
    stored: StoredRecipeVersion,
    owner_paired_002_record: Mapping[str, Any],
    exclude_worker_ids: Mapping[str, str] | None = None,
    exclude_models: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Select the first genuine execution worker from the recipe fallback ladder.

    REDI-08C2: reference-oracle and test-only workers are mechanically excluded
    from canonical transfer acceptance regardless of position.  REDI-08C4: a
    worker that already failed this exact holdout — and any worker bound to the
    same already-failed model — is excluded so the next attempt changes worker
    identity instead of resampling the same configuration.
    """

    implementation_station = str(controlled_warm_stations()[0].id)
    ladder = [
        str(worker)
        for worker in stored.recipe.worker_preferences.get(StationId(implementation_station), ())
    ]
    extended = extended_worker_registry()
    exclude_worker_ids = dict(exclude_worker_ids or {})
    exclude_models = dict(exclude_models or {})
    skipped: list[dict[str, Any]] = []
    selected: str | None = None
    selected_profile: dict[str, Any] | None = None
    selected_classification: dict[str, Any] | None = None
    for worker_id in ladder:
        profile = extended.get(worker_id)
        classification = classify_registry_worker(worker_id)
        worker_class = classification["classification"]
        if profile is None:
            skipped.append({
                "worker_id": worker_id,
                "classification": worker_class,
                "reason": "no approved WorkerProfile in the registry",
            })
            continue
        if worker_id == GPT56_TERRA_WORKER_ID:
            # The Terra completion budget is frozen per recipe version (C10).
            # Resolving it any other way would silently drift the authorized
            # configuration, so an unmapped recipe version fails closed.
            profile_version = TERRA_PROFILE_VERSION_BY_RECIPE_VERSION.get(
                int(stored.recipe.version)
            )
            if profile_version is None:
                raise HoldoutError(
                    "recipe version "
                    f"{stored.recipe.version} has no frozen Terra WorkerProfile "
                    "version binding; refusing silent completion-budget drift"
                )
            profile = gpt56_terra_holdout_worker_profile(version=profile_version)
        if implementation_station not in [str(s) for s in profile.get("station_eligibility", ())]:
            skipped.append({
                "worker_id": worker_id,
                "classification": worker_class,
                "reason": "not eligible for the implementation station",
            })
            continue
        if worker_id in exclude_worker_ids:
            skipped.append({
                "worker_id": worker_id,
                "classification": worker_class,
                "reason": exclude_worker_ids[worker_id],
            })
            continue
        model = profile.get("model")
        if model is not None and model in exclude_models:
            skipped.append({
                "worker_id": worker_id,
                "classification": worker_class,
                "reason": exclude_models[model],
            })
            continue
        if worker_class == WORKER_CLASS_INELIGIBLE:
            skipped.append({
                "worker_id": worker_id,
                "classification": worker_class,
                "reason": f"ineligible: {classification['basis']}",
            })
            continue
        if worker_class in {WORKER_CLASS_ORACLE, WORKER_CLASS_TEST_ONLY}:
            skipped.append({
                "worker_id": worker_id,
                "classification": worker_class,
                "reason": (
                    "excluded from canonical transfer acceptance: "
                    "REDI-08C2 forbids reference-oracle and test-only workers "
                    "from carrying holdout transfer evidence"
                ),
            })
            continue
        selected = worker_id
        selected_profile = dict(profile)
        selected_classification = dict(classification)
        break
    if selected is None or selected_profile is None or selected_classification is None:
        raise HoldoutError(
            "OWNER WORKER PROFILE AUTHORIZATION REQUIRED: no genuine execution "
            "WorkerProfile is available for the pinned recipe fallback ladder"
        )
    for worker_id in ladder:
        if worker_id == selected:
            continue
        classification = classify_registry_worker(worker_id)
        skipped.append({
            "worker_id": worker_id,
            "classification": classification["classification"],
            "reason": exclude_worker_ids.get(worker_id)
            or (
                "not selected: an earlier genuine execution worker carries the "
                "canonical transfer; additionally barred from canonical transfer "
                f"acceptance ({classification['classification']})"
            ),
        })
    return {
        "policy_id": WORKER_SELECTION_POLICY_ID,
        "policy_version": WORKER_SELECTION_POLICY_VERSION,
        "canonical_transfer_only": True,
        "implementation_station": implementation_station,
        "recipe_ladder": ladder,
        "ladder_source": "pinned_recipe_worker_preferences",
        "recipe_version": int(stored.recipe.version),
        "selected_worker_id": selected,
        "selected_worker_profile": selected_profile,
        "selected_worker_profile_version": selected_profile.get("version"),
        "selected_worker_classification": selected_classification,
        "worker_classifications": {item["worker_id"]: item for item in audit_worker_registry()},
        "skipped": skipped,
        "new_worker_invented": False,
        "exclusions_applied": {
            "already_failed_this_holdout": sorted(exclude_worker_ids),
            "same_model_as_failed_worker": sorted(exclude_models),
        },
        "provider_binding": {
            "provider": selected_profile.get("provider"),
            "model": selected_profile.get("model"),
            "makes_paid_provider_call": selected_profile.get("provider") is not None,
            "runtime_request_configuration": _canonical(
                selected_profile.get("runtime", {})
            ),
        },
    }


# ---------------------------------------------------------------------------
# Governed context manifests (canonical domain contract + audit view)
# ---------------------------------------------------------------------------


def _context_audit_view(
    *,
    manifest: ContextManifest,
    package: Any,
    station_role: str,
    strategy_version: int,
    rendered_request_bytes: int,
    interface_present: bool,
    leakage: bool,
    reference_leakage: bool,
) -> dict[str, Any]:
    """Audit view over a canonical ContextManifest (no parallel contract).

    The canonical domain contract carries identity/references/hash; this view
    only adds the holdout-specific audit facts as plain evidence fields.
    """

    return {
        "manifest_id": str(manifest.id),
        "canonical_contract": "ContextManifest",
        "station_role": station_role,
        "strategy_id": package.strategy_id,
        "strategy_version": strategy_version,
        "package_hash": package.package_hash,
        "rendered_hash": package.rendered_hash,
        "package_bytes": int(package.counts["bytes"]),
        "rendered_request_bytes": int(rendered_request_bytes),
        "estimated_tokens": int(package.counts["estimated_tokens"]),
        "holdout_interface_present": interface_present,
        "private_value_leakage_detected": leakage,
        "reference_solution_leakage_detected": reference_leakage,
    }


def _canonical_manifest_view(manifest: ContextManifest) -> dict[str, Any]:
    return json.loads(domain_to_json(manifest))


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _tree_hash(root: Path) -> str:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        })
    return _hash(rows)


def derive_builder_patch_output_contract(plan: Any) -> PatchOutputContract:
    """Derive the bounded builder contract from compiled canonical task scope.

    The target is not a fixture-specific adapter branch: the DAG compiler has
    already bound the implementation TaskContract to the product component's
    selected target and allowed scope.  Fail closed if those canonical sources
    disagree.
    """
    node_id = "implement_normalize"
    task = plan.tasks_by_node[node_id]
    component = dict(plan.components_by_node[node_id])
    allowed_scope = tuple(str(item) for item in task.allowed_scope)
    component_scope = tuple(str(item) for item in component.get("allowed_scope", ()))
    target = str(component.get("target_path") or "")
    selected_paths = tuple(str(item) for item in component.get("selected_paths", ()))
    if not target or target not in selected_paths:
        raise HoldoutError("compiled component target is not in canonical selected paths")
    if component_scope != allowed_scope:
        raise HoldoutError("compiled component scope disagrees with TaskContract allowed scope")
    return PatchOutputContract(
        policy_id=BUILDER_PATCH_OUTPUT_POLICY_ID,
        policy_version=BUILDER_PATCH_OUTPUT_POLICY_VERSION,
        expected_edit_count=1,
        expected_action="write",
        expected_path=target,
        content_required=True,
        extra_edits_forbidden=True,
        allowed_scope=allowed_scope,
    )


def _compile_holdout_context(
    *,
    definition: HoldoutSiblingDefinition,
    plan: Any,
    workspace_root: Path,
    budget_bytes: int,
    station_role: str,
    node_id: str,
    changed_files: Sequence[str] = (),
    diff_text: str | None = None,
    gate_summaries: Sequence[Mapping[str, Any]] = (),
    handoff_manifests: Sequence[HandoffManifest] = (),
    interface_markers: Sequence[str] = (),
    patch_output_contract: PatchOutputContract | None = None,
) -> tuple[Any, ContextManifest, dict[str, Any]]:
    component = plan.components_by_node.get(node_id)
    task = plan.tasks_by_node[node_id]
    station = plan.stations_by_node[node_id]
    selected_paths = tuple(component["selected_paths"]) if component else ()
    allowed_scope = (
        tuple(str(v) for v in component["allowed_scope"])
        if component
        else tuple(str(v) for v in task.allowed_scope)
    )
    firewall = ContextFirewall(
        allowed_classes=ContextFirewall().allowed_classes
        | frozenset({CONTEXT_CLASS_PRIOR_FAILURE_EVIDENCE, CONTEXT_CLASS_UPSTREAM_HANDOFFS})
    )
    package = compile_context(
        strategy_id=CONTEXT_STRATEGY_SELECTED_V0,
        task=task,
        station=station,
        product=definition.product,
        workspace_root=workspace_root,
        selected_paths=selected_paths,
        handoff_manifests=tuple(handoff_manifests),
        prior_failure_evidence=(),
        budget_bytes=budget_bytes,
        firewall=firewall,
        station_role=station_role,
        allowed_scope=allowed_scope,
        changed_files=tuple(changed_files),
        diff_text=diff_text,
        gate_summaries=tuple(gate_summaries),
    )
    rendered_for_leakage = package.instructions + "\n" + package.context_body
    leakage = any(marker in rendered_for_leakage for marker in definition.private_context_markers)
    markers = tuple(interface_markers)
    interface_present = all(marker in rendered_for_leakage for marker in markers)
    reference_leakage = _reference_leakage_scan(definition, rendered_for_leakage)
    request = render_live_request(
        role=station_role,
        task_id=str(task.id),
        instructions=package.instructions,
        context_body=package.context_body,
        patch_output_contract=patch_output_contract,
    )
    canonical_manifest = ContextManifest(
        task_id=task.id,
        station_id=station.id,
        product_id=definition.product.id,
        project_inputs=tuple(
            ArtifactRef(
                ref_id=f"ctx_file:{item['path']}",
                kind="file",
                location=str(item["path"]),
                sha256=str(item["sha256"]),
                source="context",
                metadata={"bytes": item["bytes"], "context_class": item["context_class"]},
            )
            for item in package.included_files
        ),
        explicit_exclusions=tuple(
            ArtifactRef(
                ref_id=f"ctx_excluded:{item['path']}",
                kind="file",
                location=str(item["path"]),
                source=None,
                metadata={"reason": item["reason"]},
            )
            for item in package.excluded_files
        ),
        upstream_handoffs=tuple(handoff.id for handoff in handoff_manifests),
        failure_evidence_included=(),
        context_budget=Budget(
            tokens_total=int(package.counts["estimated_tokens"]),
            metadata={"budget_bytes": int(budget_bytes), "strategy_id": package.strategy_id},
        ),
        deterministic_hash=package.package_hash,
        metadata={
            "rendered_hash": package.rendered_hash,
            "rendered_request_bytes": int(request["rendered_request_bytes"]),
            "counts": dict(package.counts),
            "station_role": station_role,
            "allowed_classes": sorted(package.allowed_classes),
            "task_semantic_context_hash": request["task_semantic_context_hash"],
            "full_request_hash": request["full_request_hash"],
            "output_contract": request["output_contract"],
        },
    )
    view = _context_audit_view(
        manifest=canonical_manifest,
        package=package,
        station_role=station_role,
        strategy_version=package.strategy_version,
        rendered_request_bytes=int(request["rendered_request_bytes"]),
        interface_present=interface_present,
        leakage=leakage,
        reference_leakage=reference_leakage,
    )
    view |= {
        "task_semantic_context_hash": request["task_semantic_context_hash"],
        "full_request_hash": request["full_request_hash"],
        "output_contract": request["output_contract"],
    }
    return package, canonical_manifest, view


# ---------------------------------------------------------------------------
# Governed one-shot execution boundary
# ---------------------------------------------------------------------------


def _live_provider_config_for_profile(worker_profile: Mapping[str, Any]) -> Any:
    """Build a locally validated adapter configuration from WorkerProfile data.

    Calling ``request_configuration_evidence`` is deliberately non-generating:
    no SDK/client is initialized and no provider access is attempted.  It is the
    available C6 architecture preflight; credentials and actual provider access
    remain unproven until the owner authorizes the one live completion.
    """

    from .runtime.live_provider_adapter import LiveProviderConfig

    runtime = dict(worker_profile.get("runtime", {}))
    config = LiveProviderConfig(
        env_key_name="REDI_OPENAI_API_KEY",
        model=str(worker_profile["model"]),
        timeout_s=60.0,
        max_output_tokens=int(runtime.get("max_output_tokens", MAX_OUTPUT_TOKENS)),
        temperature=runtime["temperature"] if "temperature" in runtime else 0.0,
        top_p=runtime.get("top_p"),
        reasoning_effort=runtime.get("reasoning_effort"),
        api_capability_profile=str(runtime.get("api_capability_profile", "openai_chat_legacy_v1")),
        provider_name=str(worker_profile["provider"]),
        api=str(runtime.get("api", "chat_completions")),
    )
    config.request_configuration_evidence()
    return config


class _OneShotHoldoutBoundary:
    """The single governed implementation execution slot (no automatic retry)."""

    def __init__(self) -> None:
        self.attempts = 0
        self.provider_calls = 0
        self.network_calls = 0
        self.execution_seconds = 0.0
        self._last_worker_profile: Mapping[str, Any] | None = None

    def run(
        self,
        *,
        definition: HoldoutSiblingDefinition,
        context_package: Any,
        task_id: str,
        station_id: str,
        worker_profile: Mapping[str, Any],
        workspace_root: Path,
        live: bool,
        patch_output_contract: PatchOutputContract,
        fake_builder_output: Mapping[str, Any] | None = None,
        fake_responses_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.attempts >= 1:
            raise HoldoutRetryBlocked("holdout implementation retry is forbidden (single governed slot)")
        self.attempts += 1
        self._last_worker_profile = worker_profile
        request = render_live_request(
            role="builder",
            task_id=task_id,
            instructions=context_package.instructions,
            context_body=context_package.context_body,
            patch_output_contract=patch_output_contract,
        )
        runtime_api = str(dict(worker_profile.get("runtime", {})).get("api", "chat_completions"))
        context_block: dict[str, Any] = {
            "compiled_package_bytes": context_package.counts["bytes"],
            "rendered_request_bytes": request["rendered_request_bytes"],
            "heuristic_tokens": context_package.counts["estimated_tokens"],
        }
        started = time.perf_counter()
        if not live or worker_profile.get("provider") is None:
            # Deterministic fake adapter (dry) or the local deterministic
            # executor (live, non-provider profile).  Zero provider/network.
            self.execution_seconds = time.perf_counter() - started
            if runtime_api == "responses":
                return self._run_fake_responses(
                    definition=definition,
                    context_package=context_package,
                    task_id=task_id,
                    patch_output_contract=patch_output_contract,
                    request=request,
                    context_block=context_block,
                    fake_builder_output=fake_builder_output,
                    fake_responses_result=fake_responses_result,
                )
            fake_output: Mapping[str, Any] = fake_builder_output if fake_builder_output is not None else {
                "patches": [{
                    "rel_path": patch_output_contract.expected_path,
                    "action": patch_output_contract.expected_action,
                    "content": definition.reference_patches[definition.normalize_rel_path],
                }]
            }
            parsed_output = parse_builder_output_object(
                fake_output, patch_output_contract=patch_output_contract
            )
            return {
                "patches": list(parsed_output["patches"]),
                "observed_patches": list(parsed_output["observed_patches"]),
                "output_shape": parsed_output["output_shape"],
                "provider": {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "provider": None,
                    "model": None,
                    # Declared status lives in the WorkerProfile/runtime; this is
                    # the actual observation for THIS attempt (dry: none).
                    "observed_provider_access": "not_observed_dry_rehearsal",
                },
                "context": context_block,
                "structured_output": {
                    "validated": parsed_output["safe_error"] is None,
                    "schema": "RediBuilderOutput",
                    "safe_error": parsed_output["safe_error"],
                    "source": "deterministic_fake_adapter" if not live else "local_deterministic_executor",
                },
                "provider_completion": {
                    "finish_reason": "deterministic_fake",
                    "refusal_present": False,
                    "incomplete": False,
                    "completion_token_details": {},
                    "provider_call_elapsed_seconds": self.execution_seconds,
                },
                "execution_kind": "governed_fake_adapter_slot"
                if not live
                else "governed_local_deterministic_slot",
            }
        return self._run_live_provider(
            context_package=context_package,
            task_id=task_id,
            station_id=station_id,
            worker_profile=worker_profile,
            workspace_root=workspace_root,
            context_block=context_block,
            patch_output_contract=patch_output_contract,
        )

    def _run_fake_responses(
        self,
        *,
        definition: HoldoutSiblingDefinition,
        context_package: Any,
        task_id: str,
        patch_output_contract: PatchOutputContract,
        request: Mapping[str, Any],
        context_block: Mapping[str, Any],
        fake_builder_output: Mapping[str, Any] | None,
        fake_responses_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Dry rehearsal of the exact Responses path with a fake envelope.

        The fake result flows through the SAME normalizer and structured-output
        interpreter as the live Responses adapter, so rehearsal evidence proves
        the real telemetry mapping and failure taxonomy without any network.
        """

        from .runtime.live_provider_adapter import (
            interpret_structured_output,
            normalize_responses_result,
            render_responses_request,
        )

        worker_profile = self._last_worker_profile
        responses_view = render_responses_request(
            role="builder",
            task_id=task_id,
            instructions=context_package.instructions,
            context_body=context_package.context_body,
            patch_output_contract=patch_output_contract,
            model=str(worker_profile["model"]),
            reasoning_effort=worker_profile["runtime"]["reasoning_effort"],
            max_output_tokens=int(worker_profile["runtime"]["max_output_tokens"]),
        )
        context_block = dict(context_block)
        context_block["responses_request_hash"] = responses_view["responses_request_hash"]
        if fake_responses_result is None:
            default_content = {
                "patches": [{
                    "rel_path": patch_output_contract.expected_path,
                    "action": patch_output_contract.expected_action,
                    "content": definition.reference_patches[definition.normalize_rel_path],
                }]
            }
            if fake_builder_output is not None:
                default_content = dict(fake_builder_output)
            fake_responses_result = {
                "id": "resp_fake_rehearsal_deterministic",
                "status": "completed",
                "incomplete_details": None,
                "error": None,
                "model": str(worker_profile["model"]),
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(default_content, sort_keys=True),
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 0,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 0,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 0,
                },
            }
        normalized = normalize_responses_result(
            fake_responses_result, elapsed_seconds=self.execution_seconds
        )
        interpreted = interpret_structured_output(
            normalized["output_text"],
            role="builder",
            patch_output_contract=patch_output_contract,
        )
        return {
            "patches": list(interpreted["patches"]),
            "observed_patches": list(interpreted["observed_patches"]),
            "output_shape": interpreted["output_shape"],
            "provider": {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "provider": None,
                "model": None,
                "observed_provider_access": "not_observed_dry_rehearsal",
            },
            "context": context_block,
            "structured_output": {
                "validated": interpreted["error_message"] is None,
                "schema": "RediBuilderOutput",
                "safe_error": interpreted["error_message"],
                "source": "deterministic_fake_responses_adapter",
            },
            "provider_completion": normalized["provider_completion"],
            "execution_kind": "governed_fake_adapter_slot",
        }

    def _run_live_provider(
        self,
        *,
        context_package: Any,
        task_id: str,
        station_id: str,
        worker_profile: Mapping[str, Any],
        workspace_root: Path,
        context_block: Mapping[str, Any],
        patch_output_contract: PatchOutputContract,
    ) -> dict[str, Any]:
        from .runtime.live_provider_adapter import LiveProviderAdapter

        def observe() -> None:
            self.provider_calls += 1
            self.network_calls += 1

        started = time.perf_counter()
        config = _live_provider_config_for_profile(worker_profile)
        request_configuration = config.request_configuration_evidence()
        adapter = LiveProviderAdapter(config=config, before_call=observe)
        try:
            result = adapter.run_node(
                task_id=TaskId(task_id),
                station_id=StationId(station_id),
                worker_id=WorkerId(str(worker_profile["id"])),
                context={
                    "role": "builder",
                    "instructions": context_package.instructions,
                    "context_body": context_package.context_body,
                    "patch_output_contract": patch_output_contract,
                },
                workspace_root=workspace_root,
                station_role="builder",
            )
        finally:
            self.execution_seconds = time.perf_counter() - started
        usage = result.token_usage or TokenUsage(input=0, output=0, cached_input=0)
        completion_observed = bool((result.provider_completion or {}).get("finish_reason"))
        return {
            "patches": list(result.patches),
            "observed_patches": list(result.observed_patches),
            "output_shape": dict(result.output_shape),
            "provider": {
                "calls": 1,
                "input_tokens": usage.input,
                "output_tokens": usage.output,
                "cached_input_tokens": usage.cached_input,
                "provider": str(worker_profile["provider"]),
                "model": str(worker_profile["model"]),
                "request_configuration": request_configuration,
                # Declared status stays in request_configuration (WorkerProfile
                # metadata); this is the observed outcome of THIS live call.
                "observed_provider_access": (
                    "live_completion_observed"
                    if completion_observed
                    else "no_completion_observed"
                ),
            },
            "context": dict(context_block),
            "structured_output": {
                "validated": result.error_message is None,
                "schema": "RediBuilderOutput",
                "safe_error": result.error_message,
                "source": "provider_json_schema_plus_local_validation",
            },
            "provider_completion": dict(result.provider_completion),
            "execution_kind": "governed_provider_slot",
        }


# ---------------------------------------------------------------------------
# Strict gate engine (holdout-specific expectations, no hidden-test leakage)
# ---------------------------------------------------------------------------


def _reference_leakage_scan(
    definition: HoldoutSiblingDefinition, rendered_text: str
) -> bool:
    """Fail-closed scan: no reference-implementation line may reach worker input.

    The evaluator/gate engine may compose reference fixtures; the WORKER must
    never receive them (worker input ∩ reference-solution bytes = ∅).
    """

    for patch_content in definition.reference_patches.values():
        for line in patch_content.splitlines():
            stripped = line.strip()
            if len(stripped) < 20 or stripped.startswith("#"):
                continue
            if stripped in rendered_text:
                return True
    return False


def _apply_checks(namespace: Mapping[str, Any], checks: Sequence[Mapping[str, Any]]) -> None:
    for check in checks:
        target = namespace[str(check["target"])]
        args = list(check["args"])
        if check["kind"] == "call_equals":
            actual = target(*args)
            if actual != check["expected"]:
                raise AssertionError(
                    f"{check['target']} check mismatch: expected {check['expected']!r}, got {actual!r}"
                )
        elif check["kind"] == "call_raises":
            expected_type = _EXCEPTIONS[str(check["exception"])]
            try:
                target(*args)
            except expected_type:
                continue
            raise AssertionError(
                f"{check['target']} check mismatch: expected {check['exception']} for args {args!r}"
            )
        else:
            raise HoldoutError(f"unknown check kind {check['kind']!r}")


def _import_service(workspace_root: Path, definition: HoldoutSiblingDefinition) -> dict[str, Any]:
    package = definition.key
    module_names = [package, f"{package}.normalize", f"{package}.reorder", f"{package}.service"]
    for name in module_names:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(workspace_root))
    try:
        module = importlib.import_module(f"{package}.service")
        function = getattr(module, definition.integration_function)
        return dict(function(*definition.integration_args))
    finally:
        sys.path.remove(str(workspace_root))
        for name in module_names:
            sys.modules.pop(name, None)


def _independent_reviewer_verdict(
    definition: HoldoutSiblingDefinition, mapping: Mapping[str, Any]
) -> None:
    expected_keys = set(definition.integration_expected)
    if set(mapping) != expected_keys:
        raise AssertionError(f"reviewer rejected mapping keys: {sorted(mapping)}")
    quantity = mapping["quantity"]
    pack = definition.case_pack
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0 or quantity % pack != 0:
        raise AssertionError("reviewer rejected quantity: must be a non-negative whole pack multiple")
    if quantity < mapping["safety_stock"]:
        raise AssertionError("reviewer rejected result: quantity below safety stock")
    pack_key = "cases" if "cases" in mapping else "boxes"
    if mapping[pack_key] != quantity // pack:
        raise AssertionError("reviewer rejected pack count consistency")


def evaluate_holdout_gates(
    *,
    definition: HoldoutSiblingDefinition,
    boundary_result: Mapping[str, Any] | None,
    provider_failed: str | None,
    workspace_root: Path,
    patch_output_contract: PatchOutputContract,
) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    """Run the strict ladder; every mandatory gate ends PASS / FAIL / NOT_RUN."""

    outcomes = {identity: "NOT_RUN" for identity in MANDATORY_GATES}
    reasons: dict[str, str] = {}
    patch = None
    normalize_namespace: dict[str, Any] = {}

    def fail(gate: str, message: str) -> None:
        outcomes[gate] = "FAIL"
        reasons[gate] = message

    if provider_failed is None and boundary_result is not None:
        structured = boundary_result.get("structured_output", {})
        if not structured.get("validated"):
            fail(
                "structured_output_schema",
                str(structured.get("safe_error") or "structured output was not validated"),
            )
        else:
            outcomes["structured_output_schema"] = "PASS"
    else:
        fail("structured_output_schema", provider_failed or "execution boundary not reached")

    if outcomes["structured_output_schema"] == "PASS":
        patches = list(boundary_result.get("patches", ()))
        contract_reason = patch_output_contract.validate_patches(patches)
        if contract_reason is not None:
            fail(
                "patch_output_contract",
                contract_reason,
            )
        else:
            patch = patches[0]
            outcomes["patch_output_contract"] = "PASS"

    if patch is not None:
        if not patch.rel_path.startswith(f"{definition.key}/"):
            fail("allowed_file_change_scope", "patch escaped allowed package scope")
        else:
            outcomes["allowed_file_change_scope"] = "PASS"

    if outcomes["allowed_file_change_scope"] == "PASS":
        try:
            compile(patch.content or "", definition.normalize_rel_path, "exec")
            compile(definition.reference_patches[definition.reorder_rel_path], definition.reorder_rel_path, "exec")
            compile(definition.reference_patches[definition.service_rel_path], definition.service_rel_path, "exec")
            outcomes["python_syntax_compile"] = "PASS"
        except Exception as exc:
            fail("python_syntax_compile", f"syntax failure: {type(exc).__name__}: {exc}")

    if outcomes["python_syntax_compile"] == "PASS":
        try:
            exec(patch.content or "", normalize_namespace)
            reorder_namespace: dict[str, Any] = {}
            exec(definition.reference_patches[definition.reorder_rel_path], reorder_namespace)
            _apply_checks(
                {
                    "normalize": normalize_namespace[definition.normalize_module],
                    "reorder": reorder_namespace[definition.reorder_module],
                },
                definition.public_checks,
            )
            outcomes["public_test_execution"] = "PASS"
        except Exception as exc:
            fail("public_test_execution", f"public verification failure: {type(exc).__name__}: {exc}")

    if outcomes["public_test_execution"] == "PASS":
        try:
            reorder_namespace = {}
            exec(definition.reference_patches[definition.reorder_rel_path], reorder_namespace)
            _apply_checks(
                {
                    "normalize": normalize_namespace[definition.normalize_module],
                    "reorder": reorder_namespace[definition.reorder_module],
                },
                definition.private_checks,
            )
            outcomes["private_test_execution"] = "PASS"
        except Exception as exc:
            fail("private_test_execution", f"private verification failure: {type(exc).__name__}: {exc}")

    integration_mapping: dict[str, Any] = {}
    if outcomes["private_test_execution"] == "PASS":
        try:
            digest = hashlib.sha256((patch.content or "").encode("utf-8")).hexdigest()
            if len(digest) != 64:
                raise AssertionError("artifact digest malformed")
            outcomes["handoff_artifact_validation"] = "PASS"
            (workspace_root / definition.normalize_rel_path).write_text(patch.content or "", encoding="utf-8")
            (workspace_root / definition.reorder_rel_path).write_text(
                definition.reference_patches[definition.reorder_rel_path], encoding="utf-8"
            )
            (workspace_root / definition.service_rel_path).write_text(
                definition.reference_patches[definition.service_rel_path], encoding="utf-8"
            )
            integration_mapping = _import_service(workspace_root, definition)
            if integration_mapping != dict(definition.integration_expected):
                raise AssertionError(f"integration result mismatch: {integration_mapping!r}")
            outcomes["integration_verification"] = "PASS"
            _independent_reviewer_verdict(definition, integration_mapping)
            outcomes["independent_final_reviewer"] = "PASS"
        except Exception as exc:
            message = f"integration/reviewer failure: {type(exc).__name__}: {exc}"
            if outcomes["integration_verification"] != "PASS":
                fail("integration_verification", message)
            else:
                fail("independent_final_reviewer", message)

    produced = {
        "normalize_patch_sha256": hashlib.sha256((patch.content or "").encode("utf-8")).hexdigest()
        if patch is not None
        else None,
        "normalize_patch_bytes": len((patch.content or "").encode("utf-8")) if patch is not None else 0,
        "integration_mapping": integration_mapping or None,
    }
    return outcomes, reasons, produced


# ---------------------------------------------------------------------------
# Pre-execution estimate (section-14 fields)
# ---------------------------------------------------------------------------


def pre_transfer_estimate(
    *,
    selection: HoldoutSelection,
    stored: StoredRecipeVersion,
    worker_selection: Mapping[str, Any],
    context_view: Mapping[str, Any],
    patch_output_contract: PatchOutputContract | None = None,
) -> dict[str, Any]:
    """Pre-execution estimate the future live runner must print."""

    estimated_input = int(context_view["estimated_tokens"]) + 256
    profile = dict(worker_selection["selected_worker_profile"])
    completion_budget = int(
        dict(profile.get("runtime", {})).get("max_output_tokens", MAX_OUTPUT_TOKENS)
    )
    request_configuration = (
        _live_provider_config_for_profile(profile).request_configuration_evidence()
        if profile.get("provider") is not None
        else None
    )
    estimate = {
        "holdout_product_id": str(selection.definition.product.id),
        "holdout_product_spec_sha256": selection.product_spec_sha256,
        "product_class_signature_hash": product_class_signature_hash(
            inventory_product_class(str(selection.definition.product.product_class_id))
        ),
        "recipe_id": str(stored.recipe.id),
        "recipe_version": stored.recipe.version,
        "recipe_semantic_hash": canonical_recipe_semantic_hash(stored.recipe),
        "recipe_content_hash": stored.canonical_hash,
        "lifecycle": stored.lifecycle_state.value,
        "confidence_state": {
            "tier": stored.confidence.tier,
            "evidence_state": stored.confidence.evidence_state,
            "transfer_evidence_count": stored.confidence.transfer_evidence_count,
            "probability": None,
        },
        "validity_state": stored.revalidation_state,
        "worker_profile": {"id": profile.get("id"), "kind": profile.get("kind"), "version": profile.get("version")},
        "worker_runtime": _canonical(profile.get("runtime", {})),
        "provider_request_configuration": request_configuration,
        "provider": profile.get("provider"),
        "model": profile.get("model"),
        "planned_live_provider_calls": PLANNED_LIVE_PROVIDER_CALLS,
        "expected_provider_calls_for_selected_worker": 1 if profile.get("provider") is not None else 0,
        "context_bytes": int(context_view["package_bytes"]),
        "rendered_request_bytes": int(context_view["rendered_request_bytes"]),
        "estimated_input_tokens": estimated_input,
        "max_output_tokens": completion_budget,
        "total_token_upper_bound": estimated_input + completion_budget,
        "gate_policy": {"id": WARM_START_GATE_POLICY_ID, "version": WARM_START_GATE_POLICY_VERSION},
        "no_paid_retry": True,
        "retry_policy_id": RETRY_POLICY_ID,
        "builder_output_contract": (
            patch_output_contract.to_dict() | {"canonical_hash": patch_output_contract.canonical_hash}
            if patch_output_contract is not None
            else None
        ),
    }
    if estimate["planned_live_provider_calls"] > PLANNED_LIVE_PROVIDER_CALLS:
        raise HoldoutError("planned live calls exceed the hard holdout budget")
    return estimate


# ---------------------------------------------------------------------------
# Warm transfer execution
# ---------------------------------------------------------------------------


@dataclass
class HoldoutTransferOutcome:
    root: Path
    db_path: Path
    factory_run_id: str
    result_path: Path
    verified: bool
    provider_calls: int
    network_calls: int
    dag_hash: str
    fixture_hash: str
    selection: HoldoutSelection
    worker_selection: Mapping[str, Any]
    recipe: StoredRecipeVersion
    revalidation_audit: Mapping[str, Any]
    estimate: Mapping[str, Any]
    gate_outcomes: Mapping[str, str]
    context_manifests: tuple[Mapping[str, Any], ...]
    audit: Mapping[str, bool]


def _holdout_event(
    event_type: LifecycleEventType,
    *,
    factory_run_id: str,
    product_id: str,
    recipe_id: str,
    payload: Mapping[str, Any],
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=event_type,
        entity_ids={
            "factory_run_id": factory_run_id,
            "product_id": product_id,
            "recipe_id": recipe_id,
        },
        payload=dict(payload),
        timestamp=_utcnow(),
    )


def _holdout_gate(gate_identity: str, node_id: str, outcome: str, reason: str | None) -> GateResult:
    status = {"PASS": GateStatus.PASS, "FAIL": GateStatus.FAIL, "NOT_RUN": GateStatus.BLOCKED}[outcome]
    return GateResult(
        gate_identity=gate_identity,
        gate_type=GateType.DETERMINISTIC,
        target_node_id=node_id,
        criteria=(AcceptanceCriterion(description=f"{gate_identity} must pass"),),
        status=status,
        verifier_type="deterministic_code",
        verifier_identity=f"{WARM_START_GATE_POLICY_ID}@{WARM_START_GATE_POLICY_VERSION}",
        failure_reasons=() if outcome == "PASS" else ((reason or "boundary not reached"),),
        metadata={
            "mandatory": True,
            "gate_policy_id": WARM_START_GATE_POLICY_ID,
            "gate_policy_version": WARM_START_GATE_POLICY_VERSION,
            "outcome": outcome,
            "boundary": gate_identity,
        },
        created_at=_utcnow(),
    )


def classify_holdout_failure(
    *,
    provider_failed: str | None,
    provider_completion: Mapping[str, Any],
    gate_outcomes: Mapping[str, str],
    retention_failed: bool,
) -> tuple[str, FailureCategory]:
    """Predeclared deterministic failure taxonomy (fixed BEFORE live execution).

    Ordered most-specific first; identical for Chat Completions and Responses
    results because both normalize into the same provider_completion concepts:

    1. evidence retention failure            -> EVIDENCE_RETENTION_FAILURE (tool)
    2. completion incomplete/truncated       -> PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE
    3. provider call error/exception         -> PROVIDER_CALL_FAILURE (tool)
    4. structured-output gate FAIL           -> STRUCTURED_OUTPUT_SCHEMA_FAILURE
    5. patch-contract gate FAIL              -> PATCH_OUTPUT_CONTRACT_FAILURE
    6. anything else                         -> ORDINARY_WORKER_IMPLEMENTATION_FAILURE
    """

    if retention_failed:
        return "EVIDENCE_RETENTION_FAILURE", FailureCategory.TOOL_FAILURE
    if provider_completion.get("incomplete"):
        return "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE", FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION
    if provider_failed is not None or provider_completion.get("provider_error"):
        return "PROVIDER_CALL_FAILURE", FailureCategory.TOOL_FAILURE
    if gate_outcomes["structured_output_schema"] == "FAIL":
        return "STRUCTURED_OUTPUT_SCHEMA_FAILURE", FailureCategory.DETERMINISTIC_TEST_FAILURE
    if gate_outcomes["patch_output_contract"] == "FAIL":
        return "PATCH_OUTPUT_CONTRACT_FAILURE", FailureCategory.SCOPE_VIOLATION
    return "ORDINARY_WORKER_IMPLEMENTATION_FAILURE", FailureCategory.DETERMINISTIC_TEST_FAILURE


def execute_holdout_warm_transfer(
    *,
    output_root: Path | str,
    product_class: ProductClass,
    store: RecipeStore,
    stored: StoredRecipeVersion,
    selection: HoldoutSelection,
    worker_selection: Mapping[str, Any],
    owner_paired_002_record: Mapping[str, Any],
    source_revision: str,
    live: bool = False,
    estimate_callback: Callable[[Mapping[str, Any]], None] | None = None,
    fake_builder_output: Mapping[str, Any] | None = None,
    fake_responses_result: Mapping[str, Any] | None = None,
) -> HoldoutTransferOutcome:
    """Execute the warm-only holdout transfer (harness rehearsal in dry mode)."""

    selected_profile = dict(worker_selection["selected_worker_profile"])
    selected_class = str(
        (worker_selection.get("selected_worker_classification") or {}).get(
            "classification", WORKER_CLASS_INELIGIBLE
        )
    )
    provider_bound = selected_profile.get("provider") is not None
    if live:
        if fake_builder_output is not None:
            raise HoldoutError("fake builder output is only permitted for local dry rehearsal")
        if fake_responses_result is not None:
            raise HoldoutError("fake Responses results are only permitted for local dry rehearsal")
        if not provider_bound or selected_class != WORKER_CLASS_GENUINE:
            raise HoldoutError(
                "canonical holdout transfer acceptance requires a genuine "
                "provider-backed execution worker; reference-oracle and test-only "
                "workers may not execute the owner run"
            )
        if os.environ.get("REDI_LIVE_PROVIDER") != "1":
            # The env guard protects real provider calls only.
            raise HoldoutError(
                "live provider execution requires REDI_LIVE_PROVIDER=1 and caller --live"
            )
    definition = selection.definition
    root = Path(output_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise HoldoutError(f"holdout output root must be new or empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    transfer_root = root / "holdout"
    fixture_root = transfer_root / "fixture"
    workspace_root = transfer_root / "workspace"
    artifact_root = transfer_root / "artifacts"
    for rel, content in definition.fixture_files.items():
        path = fixture_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    fixture_hash = _tree_hash(fixture_root)
    shutil.copytree(fixture_root, workspace_root)
    artifact_root.mkdir(parents=True)

    match = match_product_class(definition.product, product_class)
    if match.decision is not MatchDecision.COMPATIBLE:
        raise HoldoutError("holdout sibling is not class-compatible")
    available_stations = tuple(str(item.id) for item in controlled_warm_stations())
    eligibility = evaluate_warm_start_eligibility(
        stored=stored,
        match=match,
        available_station_ids=available_stations,
        available_worker_ids=tuple(
            str(worker)
            for workers in stored.recipe.worker_preferences.values()
            for worker in workers
        ),
        available_context_strategies=tuple(stored.recipe.context_budgets),
        available_gate_policies=(WARM_START_GATE_POLICY_ID,),
    )
    if not eligibility.eligible:
        raise HoldoutError("holdout warm-start selection blocked: " + "; ".join(eligibility.reasons))
    store.select_version(
        recipe_id=str(stored.recipe.id),
        version=stored.recipe.version,
        policy_id=WARM_TRANSFER_POLICY_ID,
        policy_version=WARM_TRANSFER_POLICY_VERSION,
        evidence={"match": match.to_dict(), "eligibility": eligibility.to_dict()},
        reason="pinned immutable recipe v2 selected for the predeclared holdout warm transfer",
    )
    semantic_hash = canonical_recipe_semantic_hash(stored.recipe)
    plan = compile_warm_start_dag(
        product=definition.product,
        product_class=product_class,
        stored=stored,
        match=match,
        eligibility=eligibility,
        recipe_identity_hash=semantic_hash,
        product_identity_hash=product_spec_hash(definition.product),
    )
    patch_output_contract = derive_builder_patch_output_contract(plan)
    revalidation_audit = recipe_v2_fallback_audit(stored)
    if (
        stored.recipe.version == HISTORICAL_RECIPE_VERSION
        and not revalidation_audit["another_eligible_worker_exists"]
    ):
        raise HoldoutError("holdout requires an eligible fallback worker; revalidation decision says none")
    ladder = [str(item) for item in revalidation_audit["declared_ladder"]]
    if str(worker_selection["selected_worker_id"]) not in ladder:
        raise HoldoutError("selected worker is not declared in the pinned recipe ladder")
    implementation_station_id = str(controlled_warm_stations()[0].id)
    context_budget = stored.recipe.context_budgets[CONTEXT_STRATEGY_SELECTED_V0]
    budget_metadata = (
        context_budget.get("metadata", {}) if isinstance(context_budget, Mapping) else context_budget.metadata
    )
    execution_policy = {
        "worker_order": [
            str(item)
            for item in stored.recipe.worker_preferences[StationId(implementation_station_id)]
        ],
        "fallback_ladder": [
            str(item)
            for item in stored.recipe.worker_fallbacks[StationId(implementation_station_id)]
        ],
        "context_strategy": CONTEXT_STRATEGY_SELECTED_V0,
        "context_budget_bytes": int(budget_metadata["budget_bytes"]),
        "retry_policy": _canonical(stored.recipe.retry_policies),
        "escalation_policy": _canonical(stored.recipe.escalation_policies),
        "parallelization": _canonical(stored.recipe.parallelization_rules),
        "gate_policy": _canonical(stored.recipe.gate_policies),
        "dag_template_id": stored.recipe.dag_template_id,
        "adaptation_hash": plan.adaptation.canonical_hash,
        "builder_output_contract": patch_output_contract.to_dict()
        | {"canonical_hash": patch_output_contract.canonical_hash},
        "source": "pinned_recipe",
    }

    db_path = transfer_root / "evidence.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    runs = FactoryRunRepository(conn, ledger)
    started_at = _utcnow()
    dag_hash = product_dag_hash(plan.dag)
    factory = FactoryRun(
        product_spec_id=definition.product.id,
        dag_id=str(plan.dag.id),
        recipe_id=stored.recipe.id,
        mode=Mode.WARM,
        final_status=RunStatus.RUNNING,
        verified=False,
        metadata={
            "acceptance": "REDI-08C",
            "execution_mode": WARM_START_KNOWN_PRODUCT,
            "warm_start_reason": "predeclared_holdout_recipe_transfer",
            "policy_id": WARM_TRANSFER_POLICY_ID,
            "policy_version": WARM_TRANSFER_POLICY_VERSION,
            "source_revision": source_revision,
            "product_spec_sha256": product_spec_hash(definition.product),
            "fixture_hash": fixture_hash,
            "recipe_selection": {
                "recipe_id": str(stored.recipe.id),
                "recipe_version": stored.recipe.version,
                "recipe_semantic_hash": semantic_hash,
                "recipe_content_hash": stored.canonical_hash,
                "lifecycle_state": stored.lifecycle_state.value,
                "validity_state": stored.revalidation_state,
                "confidence": stored.confidence.to_dict(),
                "pinned_not_latest_lookup": True,
                "validity_scope_note": (
                    "holdout sibling transfer executed under the same controlled-sibling "
                    "experimental validity scope declared by immutable recipe v2"
                ),
            },
            "revalidation_decision": revalidation_audit,
            "owner_paired_002_evidence": dict(owner_paired_002_record),
            "holdout_selection": selection.to_dict(),
            "worker_selection": _canonical(worker_selection),
            "execution_policy": execution_policy,
            "accounting_semantics": ACCOUNTING_SEMANTICS,
            "cold_start_planner_used": False,
            "cold_start_arm_executed": False,
            "provider_calls": 0,
            "network_calls": 0,
        },
        created_at=started_at,
    )
    run_id = str(factory.id)
    runs.store(factory)

    def _evidence(node_id: str, task_class: str, metadata: Mapping[str, Any], **kwargs: Any) -> RunEvidence:
        return RunEvidence(
            factory_run_id=FactoryRunId(run_id),
            product_id=definition.product.id,
            product_class_id=product_class.id,
            recipe_id=stored.recipe.id,
            recipe_version=stored.recipe.version,
            node_id=node_id,
            task_class=task_class,
            final_status=RunStatus.COMPLETED,
            metadata=dict(metadata),
            created_at=_utcnow(),
            **kwargs,
        )

    ledger.append(
        _evidence(
            "RECIPE_PIN",
            "redi08c_recipe_pin",
            {
                "evidence_kind": "redi08c_recipe_pin",
                "recipe_id": str(stored.recipe.id),
                "recipe_version": stored.recipe.version,
                "recipe_semantic_hash": semantic_hash,
                "recipe_content_hash": stored.canonical_hash,
                "lifecycle_state": stored.lifecycle_state.value,
                "validity_state": stored.revalidation_state,
                "confidence": stored.confidence.to_dict(),
                "owner_paired_002_evidence": dict(owner_paired_002_record),
                "revalidation_decision": revalidation_audit,
                "historical_replay_never_resolves_latest": True,
            },
        ),
        factory_run_id=run_id,
    )
    ledger.append(
        _holdout_event(
            LifecycleEventType.PRODUCT_ACCEPTED,
            factory_run_id=run_id,
            product_id=str(definition.product.id),
            recipe_id=str(stored.recipe.id),
            payload={
                "mode": WARM_START_KNOWN_PRODUCT,
                "holdout_product_id": str(definition.product.id),
                "holdout_product_spec_sha256": product_spec_hash(definition.product),
                "product_class_id": str(product_class.id),
                "unseen_by_prior_runs": True,
            },
        ),
        factory_run_id=run_id,
    )
    ledger.append(
        _evidence(
            "HOLDOUT_SELECTION",
            "redi08c_holdout_selection",
            {
                "evidence_kind": "redi08c_holdout_selection",
                "selection": selection.to_dict(),
                "product_class_signature_hash": product_class_signature_hash(product_class),
                "source_revision": source_revision,
                "fixture_tree_hash": fixture_hash,
                "difference_summary": holdout_difference_summary(definition, product_class),
            },
        ),
        factory_run_id=run_id,
    )
    ledger.append(
        _evidence(
            "WORKER_SELECTION",
            "redi08c_worker_selection",
            {
                "evidence_kind": "redi08c_worker_selection",
                "worker_selection": _canonical(worker_selection),
                "execution_policy": execution_policy,
            },
        ),
        factory_run_id=run_id,
    )
    ledger.append(
        _holdout_event(
            LifecycleEventType.PLAN_COMPILED,
            factory_run_id=run_id,
            product_id=str(definition.product.id),
            recipe_id=str(stored.recipe.id),
            payload={
                "graph_id": str(plan.dag.id),
                "graph_revision": plan.dag.revision,
                "graph_hash": dag_hash,
                "recipe_id": str(stored.recipe.id),
                "recipe_version": stored.recipe.version,
                "adaptation_manifest_hash": plan.adaptation.canonical_hash,
                "recipe_derived": True,
                "cold_start_planner_used": False,
            },
        ),
        factory_run_id=run_id,
    )

    builder_package, builder_manifest, builder_view = _compile_holdout_context(
        definition=definition,
        plan=plan,
        workspace_root=workspace_root,
        budget_bytes=execution_policy["context_budget_bytes"],
        station_role="builder",
        node_id="implement_normalize",
        interface_markers=(definition.builder_interface_marker,),
        patch_output_contract=patch_output_contract,
    )
    if not builder_view["holdout_interface_present"]:
        raise HoldoutError("compiled holdout context omitted the exact declared public interface")
    if builder_view["private_value_leakage_detected"]:
        raise HoldoutError("private holdout verification values leaked into worker context")
    if builder_view["reference_solution_leakage_detected"]:
        raise HoldoutError("reference-implementation bytes leaked into worker context")

    estimate = pre_transfer_estimate(
        selection=selection,
        stored=stored,
        worker_selection=worker_selection,
        context_view=builder_view,
        patch_output_contract=patch_output_contract,
    )
    if estimate_callback is not None:
        estimate_callback(estimate)
    ledger.append(
        _evidence(
            "PRE_TRANSFER_ESTIMATE",
            "redi08c_pre_transfer_estimate",
            {"evidence_kind": "redi08c_pre_transfer_estimate", "estimate": estimate},
        ),
        factory_run_id=run_id,
    )

    boundary = _OneShotHoldoutBoundary()
    selected_worker_id = str(worker_selection["selected_worker_id"])
    provider_failed: str | None = None
    boundary_result: dict[str, Any] | None = None
    gate_started = time.perf_counter()
    try:
        boundary_result = boundary.run(
            definition=definition,
            context_package=builder_package,
            task_id=str(plan.tasks_by_node["implement_normalize"].id),
            station_id=str(plan.stations_by_node["implement_normalize"].id),
            worker_profile=worker_selection["selected_worker_profile"],
            workspace_root=workspace_root,
            live=live,
            patch_output_contract=patch_output_contract,
            fake_builder_output=fake_builder_output,
            fake_responses_result=fake_responses_result,
        )
    except Exception as exc:  # governed failure persisted below; never retried
        provider_failed = f"{type(exc).__name__}: {exc}"

    # Persist a normalized safe shape before any patch-contract gate.  It has
    # counts, hashes and metadata only; it never contains raw response text,
    # reasoning, or hidden/private evaluation material.
    output_shape = dict((boundary_result or {}).get("output_shape") or {
        "schema_identity": {
            "name": "RediBuilderOutput",
            "policy_id": patch_output_contract.policy_id,
            "policy_version": patch_output_contract.policy_version,
            "policy_hash": patch_output_contract.canonical_hash,
        },
        "structured_output_validated": False,
        "parsed_patch_count": None,
        "patches": [],
    })
    ledger.append(
        _evidence(
            "PARSED_BUILDER_OUTPUT",
            "redi08c_parsed_builder_output",
            {
                "evidence_kind": "redi08c_parsed_builder_output",
                "attempt_index": boundary.attempts - 1,
                "output_contract": patch_output_contract.to_dict()
                | {"canonical_hash": patch_output_contract.canonical_hash},
                "output_shape": output_shape,
            },
        ),
        factory_run_id=run_id,
    )

    # Retain all generated content before patch-contract evaluation.  The
    # identity-derived filename deliberately never includes a model path.
    retained_artifacts: list[dict[str, Any]] = []
    retention_failures: list[dict[str, Any]] = []
    retention_by_index: dict[int, dict[str, Any]] = {}
    observations = tuple((boundary_result or {}).get("observed_patches", ()))
    for observation in observations:
        if not isinstance(observation, ParsedPatchObservation) or not isinstance(observation.content, str):
            continue
        patch_bytes = observation.content.encode("utf-8")
        digest = hashlib.sha256(patch_bytes).hexdigest()
        artifact_id = f"redi08c:attempt:{boundary.attempts - 1}:patch:{observation.index}:{digest}"
        try:
            retained_path = retain_generated_bytes_artifact(
                run_artifact_root=artifact_root,
                data=patch_bytes,
                artifact_id=artifact_id,
                expected_sha256=digest,
                expected_size=len(patch_bytes),
            )
            retained = {
                "index": observation.index,
                "artifact_id": artifact_id,
                "evidence_path": f"artifacts/{retained_path}",
                "retained_path": retained_path,
                "sha256": digest,
                "bytes": len(patch_bytes),
                "immutable_run_artifact": True,
            }
            retained_artifacts.append(retained)
            retention_by_index[observation.index] = retained
        except Exception as exc:  # retention failures must remain auditable and fail closed
            failed = {
                "index": observation.index,
                "artifact_id": artifact_id,
                "retention_error": f"{type(exc).__name__}: {exc}",
            }
            retention_failures.append(failed)
            retention_by_index[observation.index] = failed
    retained_patch: dict[str, Any] | None = (
        retained_artifacts[0] if retained_artifacts else (retention_failures[0] if retention_failures else None)
    )
    actual_patch_result = []
    for observed in output_shape.get("patches", []):
        entry = dict(observed)
        entry["retained_artifact"] = retention_by_index.get(entry.get("index"))
        actual_patch_result.append(entry)

    gate_outcomes, gate_reasons, produced = evaluate_holdout_gates(
        definition=definition,
        boundary_result=boundary_result,
        provider_failed=provider_failed,
        workspace_root=workspace_root,
        patch_output_contract=patch_output_contract,
    )
    gate_compute_seconds = max(time.perf_counter() - gate_started, 0.000001)
    gates_verified = all(gate_outcomes[identity] == "PASS" for identity in MANDATORY_GATES)
    retention_failed = bool(retention_failures)
    verified = gates_verified and not retention_failed
    provider_completion = dict((boundary_result or {}).get("provider_completion") or {})
    failure_classification, failure_category = classify_holdout_failure(
        provider_failed=provider_failed,
        provider_completion=provider_completion,
        gate_outcomes=gate_outcomes,
        retention_failed=retention_failed,
    )
    node_for_gate = {
        identity: (
            _INTEGRATION_NODE
            if identity in {"integration_verification", "independent_final_reviewer"}
            else "implement_normalize"
        )
        for identity in MANDATORY_GATES
    }
    gate_rows = [
        _holdout_gate(identity, node_for_gate[identity], gate_outcomes[identity], gate_reasons.get(identity))
        for identity in MANDATORY_GATES
    ]
    for gate in gate_rows:
        ledger.append(gate, factory_run_id=run_id)

    provider = dict(
        boundary_result.get(
            "provider",
            {
                "calls": boundary.provider_calls,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "provider": None,
                "model": None,
            },
        )
    )
    if provider_failed is not None:
        provider = {
            "calls": boundary.provider_calls,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "provider": None,
            "model": None,
        }

    attempt = RunEvidence(
        factory_run_id=FactoryRunId(run_id),
        product_id=definition.product.id,
        product_class_id=product_class.id,
        recipe_id=stored.recipe.id,
        recipe_version=stored.recipe.version,
        node_id="implement_normalize",
        station_id=plan.stations_by_node["implement_normalize"].id,
        task_class="warm_start_worker_attempt",
        worker_id=WorkerId(selected_worker_id),
        worker_version="1",
        context_manifest_id=str(builder_manifest.id),
        token_usage=TokenUsage(
            input=int(provider["input_tokens"]),
            output=int(provider["output_tokens"]),
            cached_input=int(provider["cached_input_tokens"]),
        ),
        timing=Timing(
            wall_clock_seconds=max(boundary.execution_seconds, 0.000001),
            compute_seconds=gate_compute_seconds,
        ),
        tests_executed=(
            (definition.normalize_public_test,)
            if gate_outcomes["public_test_execution"] != "NOT_RUN"
            else ()
        ),
        gate_results=tuple(
            GateResultId(str(gate.id))
            for gate in gate_rows
            if gate.target_node_id == "implement_normalize"
        ),
        retry_count=0,
        escalation=False,
        human_intervention=False,
        final_status=RunStatus.COMPLETED if verified else RunStatus.FAILED,
        failure_category=None if verified else failure_category,
        metadata={
            "evidence_kind": "redi08c_worker_attempt",
            "attempt_index": 0,
            "execution_kind": (boundary_result or {}).get("execution_kind", "governed_slot_failed"),
            "actual_worker_attempt": True,
            "recipe_consulted": True,
            "worker_policy_source": "pinned_recipe",
            "worker_classification": selected_class,
            "generates_output_from_context": bool(live),
            "worker_profile": _canonical(worker_selection["selected_worker_profile"]),
            "worker_selection": _canonical(worker_selection),
            "provider": provider,
            "provider_completion": provider_completion,
            "structured_output": (boundary_result or {}).get(
                "structured_output", {"validated": False, "safe_error": provider_failed}
            ),
            "output_contract": patch_output_contract.to_dict()
            | {"canonical_hash": patch_output_contract.canonical_hash},
            "output_shape": output_shape,
            "patch_result": actual_patch_result,
            "failure_classification": None if verified else failure_classification,
            "context_manifest": _canonical_manifest_view(builder_manifest) | builder_view,
            "gates": dict(gate_outcomes),
            "local_compute": {"gate_compute_seconds": gate_compute_seconds},
            "planned_live_provider_calls": estimate["planned_live_provider_calls"],
            "provider_retry_attempted": False,
        },
        created_at=_utcnow(),
    )
    responses_request_hash = dict((boundary_result or {}).get("context") or {}).get(
        "responses_request_hash"
    )
    if responses_request_hash is not None:
        # Responses-strategy evidence only; Chat Completions evidence bytes
        # keep their historical shape without this key.
        attempt.metadata["responses_request_hash"] = str(responses_request_hash)
    ledger.append(attempt, factory_run_id=run_id)
    worker_attempt_rows = [
        {
            "evidence_id": str(attempt.id),
            "node_id": "implement_normalize",
            "worker_id": selected_worker_id,
            "status": "completed" if verified else "failed",
            "execution_kind": (boundary_result or {}).get("execution_kind", "governed_slot_failed"),
            "provider_calls": int(provider["calls"]),
            "patch_sha256": produced["normalize_patch_sha256"],
            "retained_artifact": retained_patch,
            "retained_artifacts": retained_artifacts,
            "output_contract": patch_output_contract.to_dict()
            | {"canonical_hash": patch_output_contract.canonical_hash},
            "output_shape": output_shape,
        }
    ]

    if not verified:
        failure_reason_text = (
            "evidence retention failed" if retention_failed else provider_failed
        ) or (
            "; ".join(f"{gate}: {reason}" for gate, reason in sorted(gate_reasons.items()))
            or "mandatory gate failure"
        )
        ledger.append(
            FailureRecord(
                category=failure_category,
                origin="governed_worker_implementation",
                node_id="implement_normalize",
                task_id=plan.tasks_by_node["implement_normalize"].id,
                factory_run_id=FactoryRunId(run_id),
                details={
                    "safe_error": failure_reason_text,
                    "failed_gates": [gate for gate, state in gate_outcomes.items() if state == "FAIL"],
                    "retention_failures": retention_failures,
                },
                severity=Severity.ERROR,
                recoverable=False,
                escalation_target="owner_authorized_rerun_only",
                evidence_id=attempt.id,
                metadata={
                    "failure_class": failure_classification,
                    "transfer_failure": False,
                    "recipe_configuration_causal": False,
                    "second_provider_call_attempted": False,
                },
                created_at=_utcnow(),
            ),
            factory_run_id=run_id,
        )

    actual_patch_content = ""
    if boundary_result is not None:
        produced_patches = list(boundary_result.get("patches", ()))
        if produced_patches:
            actual_patch_content = produced_patches[0].content or ""

    reviewer_manifest: ContextManifest | None = None
    if verified:
        local_attempt = RunEvidence(
            factory_run_id=FactoryRunId(run_id),
            product_id=definition.product.id,
            product_class_id=product_class.id,
            recipe_id=stored.recipe.id,
            recipe_version=stored.recipe.version,
            node_id="implement_reorder",
            station_id=plan.stations_by_node["implement_reorder"].id,
            task_class="warm_start_worker_attempt",
            worker_id=WorkerId(selected_worker_id),
            worker_version="1",
            context_manifest_id=None,
            token_usage=TokenUsage(input=0, output=0, cached_input=0),
            retry_count=0,
            escalation=False,
            human_intervention=False,
            final_status=RunStatus.COMPLETED,
            metadata={
                "evidence_kind": "redi08c_local_deterministic_attempt",
                "execution_kind": "governed_local_deterministic",
                "actual_worker_attempt": True,
                "worker_policy_source": "pinned_recipe",
                "worker_profile": _canonical(worker_selection["selected_worker_profile"]),
                "provider": {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "provider": None,
                    "model": None,
                },
            },
            created_at=_utcnow(),
        )
        ledger.append(local_attempt, factory_run_id=run_id)
        worker_attempt_rows.append({
            "evidence_id": str(local_attempt.id),
            "node_id": "implement_reorder",
            "worker_id": selected_worker_id,
            "status": "completed",
            "execution_kind": "governed_local_deterministic",
            "provider_calls": 0,
        })
        handoffs = (
            HandoffManifest(
                producer_station_id=plan.stations_by_node["implement_normalize"].id,
                consumer_scope=str(plan.stations_by_node[_INTEGRATION_NODE].id),
                node_id="implement_normalize",
                task_id=plan.tasks_by_node["implement_normalize"].id,
                public_interfaces=(definition.normalize_interface,),
                guarantees=("normalize patch passed public and private deterministic verification",),
                metadata={"handoff_id": "handoff_normalize", "sha256": produced["normalize_patch_sha256"]},
                created_at=_utcnow(),
            ),
            HandoffManifest(
                producer_station_id=plan.stations_by_node["implement_reorder"].id,
                consumer_scope=str(plan.stations_by_node[_INTEGRATION_NODE].id),
                node_id="implement_reorder",
                task_id=plan.tasks_by_node["implement_reorder"].id,
                public_interfaces=(
                    str(plan.tasks_by_node["implement_reorder"].metadata["sibling_interface"]),
                ),
                guarantees=("reorder implementation passed public and private deterministic verification",),
                metadata={"handoff_id": "handoff_reorder"},
                created_at=_utcnow(),
            ),
        )
        _, reviewer_manifest, reviewer_view = _compile_holdout_context(
            definition=definition,
            plan=plan,
            workspace_root=workspace_root,
            budget_bytes=execution_policy["context_budget_bytes"],
            station_role="reviewer",
            node_id=_INTEGRATION_NODE,
            changed_files=(
                definition.normalize_rel_path,
                definition.reorder_rel_path,
                definition.service_rel_path,
            ),
            diff_text=(
                f"--- {definition.normalize_rel_path}\n+++ {definition.normalize_rel_path}\n"
                f"{definition.reference_patches[definition.normalize_rel_path]}"
            ),
            gate_summaries=[
                {"gate_identity": identity, "status": gate_outcomes[identity], "failure_reasons_count": 0}
                for identity in MANDATORY_GATES
                if gate_outcomes[identity] == "PASS"
                and identity not in {"integration_verification", "independent_final_reviewer"}
            ],
            handoff_manifests=handoffs,
            interface_markers=(definition.reviewer_interface_marker,),
        )
        if not reviewer_view["holdout_interface_present"]:
            raise HoldoutError("reviewer context omitted the integration interface")
        if reviewer_view["private_value_leakage_detected"]:
            raise HoldoutError("private holdout values leaked into reviewer context")
        # Reference-line presence in the REVIEWER context is expected whenever
        # the produced diff is reviewed; oracle isolation is enforced on the
        # implementation worker's input, which is the fail-closed scan above.
        reviewer_attempt = RunEvidence(
            factory_run_id=FactoryRunId(run_id),
            product_id=definition.product.id,
            product_class_id=product_class.id,
            recipe_id=stored.recipe.id,
            recipe_version=stored.recipe.version,
            node_id=_INTEGRATION_NODE,
            station_id=plan.stations_by_node[_INTEGRATION_NODE].id,
            task_class="warm_start_independent_integration_review",
            worker_id=WorkerId(INDEPENDENT_REVIEWER_WORKER),
            worker_version="1",
            context_manifest_id=str(reviewer_manifest.id),
            token_usage=TokenUsage(input=0, output=0, cached_input=0),
            retry_count=0,
            escalation=False,
            human_intervention=False,
            final_status=RunStatus.COMPLETED,
            metadata={
                "evidence_kind": "redi08c_independent_integration_review",
                "execution_kind": "governed_local_independent_reviewer",
                "actual_worker_attempt": True,
                "worker_policy_source": "pinned_recipe",
                "worker_profile": _canonical(worker_profiles()[INDEPENDENT_REVIEWER_WORKER]),
                "integration_mapping": produced["integration_mapping"],
                "context_manifest": _canonical_manifest_view(reviewer_manifest) | reviewer_view,
                "provider": {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "provider": None,
                    "model": None,
                },
            },
            created_at=_utcnow(),
        )
        ledger.append(reviewer_attempt, factory_run_id=run_id)
        worker_attempt_rows.append({
            "evidence_id": str(reviewer_attempt.id),
            "node_id": _INTEGRATION_NODE,
            "worker_id": INDEPENDENT_REVIEWER_WORKER,
            "status": "completed",
            "execution_kind": "governed_local_independent_reviewer",
            "provider_calls": 0,
        })

    wall = max(time.perf_counter() - gate_started, 0.000001)
    if verified:
        ledger.append(
            _holdout_event(
                LifecycleEventType.PRODUCT_VERIFIED,
                factory_run_id=run_id,
                product_id=str(definition.product.id),
                recipe_id=str(stored.recipe.id),
                payload={
                    "graph_id": str(plan.dag.id),
                    "graph_revision": plan.dag.revision,
                    "graph_hash": dag_hash,
                    "recipe_id": str(stored.recipe.id),
                    "recipe_version": stored.recipe.version,
                    "adaptation_manifest_hash": plan.adaptation.canonical_hash,
                    "gate_policy_id": WARM_START_GATE_POLICY_ID,
                    "all_mandatory_gates_passed": True,
                    "independent_reviewer": INDEPENDENT_REVIEWER_WORKER,
                    "selected_worker": selected_worker_id,
                    "provider_calls": int(provider["calls"]),
                    "transfer_claimed_as_advantage": False,
                },
            ),
            factory_run_id=run_id,
        )

    snapshot = {
        "run_kind": "redi08c_holdout_warm_transfer",
        "policy_id": WARM_TRANSFER_POLICY_ID,
        "policy_version": WARM_TRANSFER_POLICY_VERSION,
        "source_revision": source_revision,
        "holdout_product_id": str(definition.product.id),
        "holdout_product_spec_sha256": product_spec_hash(definition.product),
        "fixture_hash": fixture_hash,
        "dag_hash": dag_hash,
        "dag_snapshot": dag_snapshot(plan.dag),
        "dag_metadata": _canonical(plan.dag.metadata),
        "recipe": {
            "recipe_id": str(stored.recipe.id),
            "recipe_version": stored.recipe.version,
            "recipe_semantic_hash": semantic_hash,
            "recipe_content_hash": stored.canonical_hash,
            "parent_recipe_version": stored.provenance.get("parent_recipe_version"),
            "parent_recipe_semantic_hash": stored.provenance.get("parent_recipe_semantic_hash"),
            "lineage_v2_recipe_semantic_hash": stored.provenance.get(
                "lineage_v2_recipe_semantic_hash", HISTORICAL_RECIPE_SEMANTIC_HASH
            ),
            "provenance": _canonical(stored.provenance),
            "metadata": _canonical(stored.recipe.metadata),
            "lifecycle_state": stored.lifecycle_state.value,
            "validity_state": stored.revalidation_state,
            "confidence": stored.confidence.to_dict(),
        },
        "owner_paired_002_evidence": dict(owner_paired_002_record),
        "revalidation_decision": revalidation_audit,
        "holdout_selection": selection.to_dict(),
        "worker_selection": _canonical(worker_selection),
        "worker_legitimacy_audit": {
            "policy_id": WORKER_CLASSIFICATION_POLICY_ID,
            "policy_version": WORKER_CLASSIFICATION_POLICY_VERSION,
            "registry": [dict(item) for item in audit_worker_registry()],
            "genuine_worker_selected": selected_class == WORKER_CLASS_GENUINE,
        },
        "execution_policy": execution_policy,
        "adaptation_manifest": plan.adaptation.to_dict(),
        "pre_transfer_estimate": estimate,
        "context_manifests": [
            _canonical_manifest_view(builder_manifest) | builder_view
        ]
        + (
            [_canonical_manifest_view(reviewer_manifest) | reviewer_view]
            if reviewer_manifest is not None
            else []
        ),
        "gate_records": [
            {
                "gate_identity": identity,
                "status": gate_outcomes[identity],
                "node_id": node_for_gate[identity],
                "reason": gate_reasons.get(identity),
            }
            for identity in MANDATORY_GATES
        ],
        "worker_attempts": worker_attempt_rows,
        "patch_retention": {
            "policy_id": "redi08c_governed_patch_retention_v1",
            "retained": retained_patch,
            "retained_artifacts": retained_artifacts,
            "retention_failures": retention_failures,
            "retention_required_for_acceptance": True,
            "binds_worker_attempt": True,
            "retains_failed_attempts": True,
            "excludes_provider_response_object": True,
            "excludes_reasoning": True,
            "excludes_hidden_test_data": True,
        },
        "integration_mapping": produced["integration_mapping"],
        "builder_output_contract": patch_output_contract.to_dict()
        | {"canonical_hash": patch_output_contract.canonical_hash},
        "parsed_builder_output": output_shape,
        "provider_completion": provider_completion,
        "failure_classification": None if verified else failure_classification,
        "verified": verified,
        **(
            {"responses_request_hash": str(responses_request_hash)}
            if responses_request_hash is not None
            else {}
        ),
        "provider_calls": int(provider["calls"]),
        "network_calls": boundary.network_calls,
        "acceptance_semantics": (
            ACCEPTANCE_GENUINE_TRANSFER if live else ACCEPTANCE_HARNESS_REHEARSAL
        ),
        "counts_as_transfer_evidence": bool(live and verified),
        "provider_live_acceptance": bool(live),
        "implementation_source": (
            "worker_generated_from_governed_context"
            if live
            else "predeclared_reference_implementation_harness_rehearsal_only"
        ),
        "recipe_promoted": False,
        "recipe_store_mutated_after_pin": False,
        "recipe_evidence_state": {
            "source_verified_runs_count": stored.confidence.source_verified_runs_count,
            "failed_transfer_attempts_count": 1,
            "failed_transfer_attempt_refs": [OWNER_PAIRED_002_PAIR_ID],
            "successful_holdout_transfer_runs_before_this_attempt": stored.confidence.transfer_evidence_count,
            "this_holdout_attempt_verified": verified,
            "recipe_metadata_mutated": False,
            "probability": None,
        },
        "claims": {
            "recipe_advantage_claimed": False,
            "recipe_stability_claimed": False,
            "statistical_generalization_claimed": False,
            "best_worker_claimed": False,
            "mcw_claimed": False,
            "cost_optimality_claimed": False,
            "worker_capability_transfer_claimed": bool(live and verified),
            "genuine_worker_execution": bool(live and verified),
            "harness_rehearsal_is_not_transfer_evidence": not live,
            "provider_live_acceptance_claimed": False,
            "recipe_pipeline_transfer_on_unseen_holdout": verified,
            "transfer_to_one_unseen_holdout_sibling": verified,
        },
        "replay_dependencies": {
            "workers": 0,
            "provider_calls": 0,
            "network": 0,
            "writes": 0,
            "recipe_reselection": False,
            "current_config_influence": False,
            "current_worker_registry": False,
        },
    }
    ledger.append(
        RunEvidence(
            factory_run_id=FactoryRunId(run_id),
            product_id=definition.product.id,
            product_class_id=product_class.id,
            recipe_id=stored.recipe.id,
            recipe_version=stored.recipe.version,
            node_id="HOLDOUT_TRANSFER_SNAPSHOT",
            task_class="redi08c_transfer_snapshot",
            token_usage=TokenUsage(
                input=int(provider["input_tokens"]),
                output=int(provider["output_tokens"]),
                cached_input=int(provider["cached_input_tokens"]),
            ),
            timing=Timing(wall_clock_seconds=wall, compute_seconds=gate_compute_seconds),
            retry_count=0,
            escalation=False,
            human_intervention=False,
            final_status=RunStatus.COMPLETED if verified else RunStatus.FAILED,
            metadata={"evidence_kind": "redi08c_transfer_snapshot", "snapshot": _canonical(snapshot)},
            created_at=_utcnow(),
        ),
        factory_run_id=run_id,
    )
    runs.close_run(
        run_id,
        final_status=RunStatus.COMPLETED if verified else RunStatus.FAILED,
        verified=verified,
        total_tokens=TokenUsage(
            input=int(provider["input_tokens"]),
            output=int(provider["output_tokens"]),
            cached_input=int(provider["cached_input_tokens"]),
        ),
        timing=Timing(
            started_at=started_at,
            ended_at=_utcnow(),
            wall_clock_seconds=wall,
            compute_seconds=gate_compute_seconds,
        ),
    )
    conn.close()

    result = _canonical(snapshot) | {
        "factory_run_id": run_id,
        "db_path": str(db_path),
        "difference_summary": holdout_difference_summary(definition, product_class),
        "recipe_provenance_chain": {
            "holdout_factory_run": run_id,
            "pinned_recipe": {
                "recipe_id": str(stored.recipe.id),
                "recipe_version": stored.recipe.version,
                "recipe_semantic_hash": semantic_hash,
                "recipe_content_hash": stored.canonical_hash,
            },
            "recipe_lineage_source_factory_run": str(stored.provenance["source_factory_run_id"]),
            "source_candidate_canonical_hash": str(stored.provenance["source_candidate_canonical_hash"]),
            "source_product_verified_event_id": str(stored.provenance["source_product_verified_event_id"]),
            "revalidation_evidence_refs": [OWNER_PAIRED_002_PAIR_ID],
            "dangling_refs": [],
        },
    }
    result["canonical_hash"] = _hash(result)
    result_path = root / "holdout_result.json"
    result_path.write_text(
        json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    context_manifests = [builder_view] + (
        [reviewer_view] if reviewer_manifest is not None else []
    )
    outcome = HoldoutTransferOutcome(
        root=root,
        db_path=db_path,
        factory_run_id=run_id,
        result_path=result_path,
        verified=verified,
        provider_calls=int(provider["calls"]),
        network_calls=boundary.network_calls,
        dag_hash=dag_hash,
        fixture_hash=fixture_hash,
        selection=selection,
        worker_selection=worker_selection,
        recipe=stored,
        revalidation_audit=revalidation_audit,
        estimate=estimate,
        gate_outcomes=gate_outcomes,
        context_manifests=tuple(context_manifests),
        audit={},
    )
    outcome.audit = audit_holdout_transfer(outcome=outcome, strict_identity=True)
    return outcome


# ---------------------------------------------------------------------------
# Replay + audit
# ---------------------------------------------------------------------------


def replay_holdout(*, db_path: Path | str, factory_run_id: str) -> dict[str, Any]:
    """Read-only historical replay; never consults the current recipe store."""

    reader = ReplayReader(Path(db_path).resolve())
    try:
        replay = reader.replay_factory_run(FactoryRunId(factory_run_id))
    finally:
        reader.close()
    if replay.integrity_status != "OK":
        raise HoldoutError(f"holdout replay integrity is {replay.integrity_status}")
    records = [
        item
        for item in replay.run_evidences
        if (item.metadata or {}).get("evidence_kind") == "redi08c_transfer_snapshot"
    ]
    if len(records) != 1:
        raise HoldoutError("holdout replay requires exactly one canonical transfer snapshot")
    return _canonical(records[0].metadata["snapshot"])


def replay_holdout_result(path: Path | str) -> dict[str, Any]:
    result_path = Path(path).resolve()
    before = result_path.stat()
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    expected = payload.pop("canonical_hash")
    if _hash(payload) != expected:
        raise HoldoutError("holdout result canonical hash mismatch")
    after = result_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise HoldoutError("holdout result replay was not read-only")
    return _canonical(payload | {"canonical_hash": expected})


def audit_holdout_transfer(
    *, outcome: HoldoutTransferOutcome, strict_identity: bool = True
) -> dict[str, bool]:
    snapshot_one = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    snapshot_two = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    result_one = replay_holdout_result(outcome.result_path)
    result_two = replay_holdout_result(outcome.result_path)
    gate_records = {item["gate_identity"]: item["status"] for item in snapshot_one["gate_records"]}
    builder_manifests = [
        item for item in snapshot_one["context_manifests"] if item["station_role"] == "builder"
    ]
    component_nodes = {
        node["node_id"]: node
        for node in snapshot_one["dag_snapshot"]["nodes"]
    }
    difference = result_one["difference_summary"]
    pinned_version = snapshot_one["recipe"]["recipe_version"]
    identity_checks = {
        "source_lineage_is_accepted_run": (
            result_one["recipe_provenance_chain"]["recipe_lineage_source_factory_run"]
            == ACCEPTED_SOURCE_FACTORY_RUN_ID
        ),
    }
    if strict_identity:
        identity_checks["recipe_lineage_identity"] = (
            snapshot_one["recipe"]["recipe_semantic_hash"] == HISTORICAL_RECIPE_SEMANTIC_HASH
            if pinned_version == HISTORICAL_RECIPE_VERSION
            else (
                snapshot_one["recipe"]["parent_recipe_semantic_hash"]
                == HISTORICAL_RECIPE_SEMANTIC_HASH
                and pinned_version == HISTORICAL_RECIPE_VERSION + 1
            )
            if pinned_version == HISTORICAL_RECIPE_VERSION + 1
            else (
                pinned_version == HISTORICAL_RECIPE_VERSION + 2
                and snapshot_one["recipe"]["parent_recipe_version"]
                == HISTORICAL_RECIPE_VERSION + 1
                and snapshot_one["recipe"]["lineage_v2_recipe_semantic_hash"]
                == HISTORICAL_RECIPE_SEMANTIC_HASH
            )
            if pinned_version == HISTORICAL_RECIPE_VERSION + 2
            else (
                pinned_version == HISTORICAL_RECIPE_VERSION + 3
                and snapshot_one["recipe"]["parent_recipe_version"]
                == HISTORICAL_RECIPE_VERSION + 2
                and snapshot_one["recipe"]["lineage_v2_recipe_semantic_hash"]
                == HISTORICAL_RECIPE_SEMANTIC_HASH
            )
            if pinned_version == HISTORICAL_RECIPE_VERSION + 3
            else (
                pinned_version == HISTORICAL_RECIPE_VERSION + 4
                and snapshot_one["recipe"]["parent_recipe_version"]
                == HISTORICAL_RECIPE_VERSION + 3
                and snapshot_one["recipe"]["lineage_v2_recipe_semantic_hash"]
                == HISTORICAL_RECIPE_SEMANTIC_HASH
            )
            if pinned_version == HISTORICAL_RECIPE_VERSION + 4
            else (
                pinned_version == HISTORICAL_RECIPE_VERSION + 5
                and snapshot_one["recipe"]["parent_recipe_version"]
                == HISTORICAL_RECIPE_VERSION + 4
                and snapshot_one["recipe"]["lineage_v2_recipe_semantic_hash"]
                == HISTORICAL_RECIPE_SEMANTIC_HASH
            )
        )
    return {
        "holdout_same_class_compatible": (
            snapshot_one["holdout_selection"]["match"]["decision"] == "COMPATIBLE"
        ),
        "holdout_unseen_by_prior_runs": (
            snapshot_one["holdout_selection"]["holdout_product_spec_sha256"]
            not in set(snapshot_one["holdout_selection"]["prior_used_registry"].values())
        ),
        "holdout_selection_policy_predeclared": (
            snapshot_one["holdout_selection"]["policy_id"] == HOLDOUT_SELECTION_POLICY_ID
            and snapshot_one["holdout_selection"]["creation_policy"]
            .startswith("predeclared ordered catalog")
        ),
        "holdout_materially_different": (
            difference["materially_different_vs_source"]
            and difference["materially_different_vs_paired_sibling"]
            and difference["class_invariants_equal"]
        ),
        "recipe_pinned_shadow_with_v2_lineage": (
            snapshot_one["recipe"]["recipe_id"] == HISTORICAL_RECIPE_ID
            and snapshot_one["recipe"]["recipe_version"] in {2, 3, 4, 5, 6, 7}
            and snapshot_one["recipe"]["lifecycle_state"] == RecipeRung.SHADOW.value
            and snapshot_one["recipe"]["validity_state"] == "VALID_FOR_CONTROLLED_EXPERIMENT"
            and (
                snapshot_one["recipe"]["recipe_version"] == 2
                or snapshot_one["recipe"]["parent_recipe_semantic_hash"]
                == HISTORICAL_RECIPE_SEMANTIC_HASH
                or snapshot_one["recipe"]["lineage_v2_recipe_semantic_hash"]
                == HISTORICAL_RECIPE_SEMANTIC_HASH
            )
        )
        and all(identity_checks.values() or [True]),
        "recipe_version_policy_correct": (
            snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION
            and snapshot_one["revalidation_decision"]["decision"]
            == "REUSE_RECIPE_V2_FALLBACK_LADDER"
            or snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 1
            and snapshot_one["revalidation_decision"]["decision"]
            == "V3_WORKER_POLICY_REVALIDATION_PINNED"
            or snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 2
            and snapshot_one["revalidation_decision"]["decision"]
            == "V4_TERRA_WORKER_POLICY_REVALIDATION_PINNED"
            or snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 3
            and snapshot_one["revalidation_decision"]["decision"]
            == "V5_COMPLETION_BUDGET_REVALIDATION_PINNED"
            or snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 4
            and snapshot_one["revalidation_decision"]["decision"]
            == "V6_REASONING_REVALIDATION_PINNED"
            or snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 5
            and snapshot_one["revalidation_decision"]["decision"]
            == "V7_RESPONSES_STRATEGY_REVALIDATION_PINNED"
        ),
        "owner_paired_002_evidence_recorded": (
            snapshot_one["owner_paired_002_evidence"]["classification"] == OWNER_PAIRED_002_CLASSIFICATION
            and snapshot_one["owner_paired_002_evidence"]["failure_class"] == OWNER_PAIRED_002_FAILURE_CLASS
        ),
        "worker_selected_from_recipe_ladder": (
            snapshot_one["worker_selection"]["selected_worker_id"]
            in snapshot_one["worker_selection"]["recipe_ladder"]
            and snapshot_one["worker_selection"]["ladder_source"]
            == "pinned_recipe_worker_preferences"
        ),
        "no_new_worker_invented": (
            snapshot_one["worker_selection"]["selected_worker_id"]
            in extended_worker_registry()
            and snapshot_one["worker_selection"]["new_worker_invented"] is False
        ),
        "recipe_policy_execution": (
            snapshot_one["execution_policy"]["source"] == "pinned_recipe"
            and snapshot_one["execution_policy"]["context_strategy"] == CONTEXT_STRATEGY_SELECTED_V0
            and snapshot_one["execution_policy"]["retry_policy"]["policy_id"]
            == "cold_start_deterministic_exploration_v0"
            and snapshot_one["execution_policy"]["gate_policy"]["policy_id"] == WARM_START_GATE_POLICY_ID
            and snapshot_one["dag_metadata"]["cold_start_planner_used"] is False
            and component_nodes["implement_normalize"]["metadata"]["recipe_version"]
            == snapshot_one["recipe"]["recipe_version"]
        ),
        "recipe_derived_dag": (
            snapshot_one["dag_metadata"]["source_recipe_version"]
            == snapshot_one["recipe"]["recipe_version"]
            and snapshot_one["dag_metadata"]["source_recipe_hash"]
            == snapshot_one["recipe"]["recipe_semantic_hash"]
        ),
        "context_compiler_used": (
            len(builder_manifests) == 1
            and builder_manifests[0]["strategy_id"] == CONTEXT_STRATEGY_SELECTED_V0
            and builder_manifests[0]["package_bytes"] > 0
            and builder_manifests[0]["rendered_request_bytes"] > 0
            and builder_manifests[0]["holdout_interface_present"]
        ),
        "no_private_context_leakage": all(
            item["private_value_leakage_detected"] is False
            for item in snapshot_one["context_manifests"]
        ),
        "gate_records_complete": set(gate_records) == set(MANDATORY_GATES),
        "all_required_gates_passed": all(status == "PASS" for status in gate_records.values()),
        "product_verified": snapshot_one["verified"] is True,
        "integration_reviewer_pass": (
            gate_records["integration_verification"] == "PASS"
            and gate_records["independent_final_reviewer"] == "PASS"
        ),
        "hard_live_budget": (
            snapshot_one["pre_transfer_estimate"]["planned_live_provider_calls"]
            <= PLANNED_LIVE_PROVIDER_CALLS
            and snapshot_one["pre_transfer_estimate"]["no_paid_retry"] is True
        ),
        "external_calls_within_budget": (
            (
                snapshot_one["provider_calls"] == 0
                and snapshot_one["network_calls"] == 0
                and outcome.provider_calls == 0
                and outcome.network_calls == 0
            )
            if snapshot_one["acceptance_semantics"] == ACCEPTANCE_HARNESS_REHEARSAL
            else (
                snapshot_one["provider_calls"] <= PLANNED_LIVE_PROVIDER_CALLS
                and snapshot_one["network_calls"] <= PLANNED_LIVE_PROVIDER_CALLS
            )
        ),
        "replay_x2_snapshot": snapshot_one == snapshot_two,
        "replay_x2_result": result_one == result_two,
        "replay_zero_execution": (
            snapshot_one["replay_dependencies"]
            == {
                "workers": 0,
                "provider_calls": 0,
                "network": 0,
                "writes": 0,
                "recipe_reselection": False,
                "current_config_influence": False,
                "current_worker_registry": False,
            }
        ),
        "recipe_not_promoted": (
            snapshot_one["recipe_promoted"] is False
            and snapshot_one["recipe"]["lifecycle_state"] == RecipeRung.SHADOW.value
        ),
        "provenance_chain_complete": (
            result_one["recipe_provenance_chain"]["pinned_recipe"]["recipe_semantic_hash"]
            == snapshot_one["recipe"]["recipe_semantic_hash"]
            and result_one["recipe_provenance_chain"]["holdout_factory_run"]
            == outcome.factory_run_id
            and result_one["recipe_provenance_chain"]["dangling_refs"] == []
            and result_one["recipe_provenance_chain"]["revalidation_evidence_refs"]
            == [OWNER_PAIRED_002_PAIR_ID]
        ),
        "negative_evidence_preserved": (
            OWNER_PAIRED_002_PAIR_ID
            in snapshot_one["recipe_evidence_state"]["failed_transfer_attempt_refs"]
            and snapshot_one["recipe_evidence_state"]["failed_transfer_attempts_count"] == 1
            and snapshot_one["recipe_evidence_state"]["recipe_metadata_mutated"] is False
        ),
        "no_claims_beyond_single_transfer": all(
            snapshot_one["claims"][claim] is False
            for claim in (
                "recipe_advantage_claimed",
                "recipe_stability_claimed",
                "statistical_generalization_claimed",
                "best_worker_claimed",
                "mcw_claimed",
                "cost_optimality_claimed",
                "provider_live_acceptance_claimed",
            )
        ),
        "canonical_context_contract_used": all(
            item.get("canonical_contract") == "ContextManifest"
            and item.get("__type__") == "ContextManifest"
            and str(item.get("id", "")).startswith("context_manifest_")
            and item.get("deterministic_hash")
            for item in snapshot_one["context_manifests"]
        ),
        "acceptance_semantics_bounded": (
            snapshot_one["acceptance_semantics"]
            in {ACCEPTANCE_HARNESS_REHEARSAL, ACCEPTANCE_GENUINE_TRANSFER}
            and snapshot_one["counts_as_transfer_evidence"]
            == (snapshot_one["acceptance_semantics"] == ACCEPTANCE_GENUINE_TRANSFER and snapshot_one["verified"])
            and (
                snapshot_one["acceptance_semantics"] == ACCEPTANCE_GENUINE_TRANSFER
                or snapshot_one["provider_calls"] == 0
            )
        ),
        "worker_profile_slot_consistency": (
            snapshot_one["pre_transfer_estimate"]["expected_provider_calls_for_selected_worker"]
            >= snapshot_one["provider_calls"]
        ),
        "genuine_worker_selected": (
            snapshot_one["worker_selection"]["selected_worker_classification"]["classification"]
            == WORKER_CLASS_GENUINE
            and snapshot_one["worker_selection"]["canonical_transfer_only"] is True
            and snapshot_one["worker_legitimacy_audit"]["genuine_worker_selected"] is True
        ),
        "alternate_worker_genuinely_different": (
            snapshot_one["worker_selection"]["selected_worker_id"]
            != GENUINE_HOLDOUT_FAILURE["failed_worker"]
            and snapshot_one["worker_selection"]["provider_binding"]["model"]
            != GENUINE_HOLDOUT_FAILURE["model"]
        ),
        "previous_failed_worker_excluded_this_attempt": (
            all(
                failed_worker not in snapshot_one["worker_selection"]["recipe_ladder"]
                for failed_worker in (
                    GENUINE_HOLDOUT_FAILURE["failed_worker"],
                    ALTERNATE_GENUINE_HOLDOUT_FAILURE["failed_worker"],
                )
            )
            and snapshot_one["recipe"]["recipe_version"]
            in {
                HISTORICAL_RECIPE_VERSION + 2,
                HISTORICAL_RECIPE_VERSION + 3,
                HISTORICAL_RECIPE_VERSION + 4,
                HISTORICAL_RECIPE_VERSION + 5,
            }
            if snapshot_one["recipe"]["recipe_version"]
            in {
                HISTORICAL_RECIPE_VERSION + 2,
                HISTORICAL_RECIPE_VERSION + 3,
                HISTORICAL_RECIPE_VERSION + 4,
                HISTORICAL_RECIPE_VERSION + 5,
            }
            else any(
                item["worker_id"] == GENUINE_HOLDOUT_FAILURE["failed_worker"]
                for item in snapshot_one["worker_selection"]["skipped"]
            )
        ),
        "harness_rehearsal_uses_fake_adapter": (
            snapshot_one["acceptance_semantics"] != ACCEPTANCE_HARNESS_REHEARSAL
            or (
                snapshot_one["implementation_source"]
                == "predeclared_reference_implementation_harness_rehearsal_only"
                and snapshot_one["worker_attempts"][0]["execution_kind"]
                == "governed_fake_adapter_slot"
            )
        ),
        "context_frozen_vs_failed_run": any(
            item.get("station_role") == "builder"
            and item.get("rendered_hash") == FROZEN_BUILDER_RENDERED_HASH
            for item in snapshot_one["context_manifests"]
        ),
        "no_prior_worker_failure_hint_in_builder_context": all(
            not item.get("failure_evidence_included")
            and not item.get("previous_decisions")
            for item in snapshot_one["context_manifests"]
            if item.get("station_role") == "builder"
        ),
        "v4_terra_only_worker_policy": (
            snapshot_one["worker_selection"]["recipe_ladder"] == [GPT56_TERRA_WORKER_ID]
            and snapshot_one["execution_policy"]["fallback_ladder"] == [GPT56_TERRA_WORKER_ID]
            and snapshot_one["worker_selection"]["selected_worker_id"] == GPT56_TERRA_WORKER_ID
            if snapshot_one["recipe"]["recipe_version"]
            in {
                HISTORICAL_RECIPE_VERSION + 2,
                HISTORICAL_RECIPE_VERSION + 3,
                HISTORICAL_RECIPE_VERSION + 4,
                HISTORICAL_RECIPE_VERSION + 5,
            }
            else True
        ),
        "v4_required_provenance": (
            snapshot_one["recipe"]["provenance"]["revalidation_evidence"]["cross_worker_audit"]
            == REPEATED_WORKER_FAILURE_AUDIT
            and snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["owner_authorization"]["authorized_worker_profile"] == GPT56_TERRA_WORKER_ID
            and snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["gpt41_genuine_failure"]["model"] == GENUINE_HOLDOUT_FAILURE["model"]
            and snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["gpt4o_mini_genuine_failure"]["model"]
            == ALTERNATE_GENUINE_HOLDOUT_FAILURE["model"]
            if snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 2
            else True
        ),
        "v5_required_provenance": (
            snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["historical_terra_truncated_run"] == HISTORICAL_TERRA_TRUNCATED_RUN
            and snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["budget_audit_verdict"] == "COMPLETION_BUDGET_REMEDIATION_REQUIRED"
            and snapshot_one["recipe"]["metadata"]["completion_budget_revalidation"]
            == {
                "policy": "REDI-08C10",
                "parameter": "max_completion_tokens",
                "prior_tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[1],
                "tokens": TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[2],
                "hard_ceiling_not_expected_usage": True,
                "worker_profile_id": GPT56_TERRA_WORKER_ID,
                "worker_profile_version": 2,
                "reasoning_effort_unchanged": "medium",
            }
            and snapshot_one["recipe"]["parent_recipe_version"]
            == HISTORICAL_RECIPE_VERSION + 2
            if snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 3
            else True
        ),
        "v6_required_provenance": (
            snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["historical_terra_saturation_runs"] == HISTORICAL_TERRA_SATURATION_RUNS
            and snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["budget_audit_verdict"] == "REASONING_BUDGET_SATURATION_REPEATED"
            and snapshot_one["recipe"]["metadata"]["reasoning_revalidation"]
            == REASONING_REVALIDATION_METADATA
            and snapshot_one["recipe"]["parent_recipe_version"]
            == HISTORICAL_RECIPE_VERSION + 3
            if snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 4
            else True
        ),
        "v7_required_provenance": (
            snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["historical_terra_chat_completions_runs"]["run_c_low_2048"]
            == HISTORICAL_TERRA_LOW_SATURATION_RUN
            and snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["execution_strategy_audit"]["audit_verdict"] == "EXECUTION_STRATEGY_REVIEW_REQUIRED"
            and snapshot_one["recipe"]["provenance"]["revalidation_evidence"]
            ["execution_strategy_audit"]["reasoning_effort_verdict"]
            == "LOW_EFFORT_NO_MEANINGFUL_CHANGE"
            and snapshot_one["recipe"]["metadata"]["responses_strategy_revalidation"]
            == RESPONSES_STRATEGY_REVALIDATION_METADATA
            and snapshot_one["recipe"]["parent_recipe_version"]
            == HISTORICAL_RECIPE_VERSION + 4
            if snapshot_one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 5
            else True
        ),
        "completion_budget_binding": (
            (
                snapshot_one["pre_transfer_estimate"]["max_output_tokens"]
                == TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[
                    TERRA_PROFILE_VERSION_BY_RECIPE_VERSION[
                        snapshot_one["recipe"]["recipe_version"]
                    ]
                ]
                and snapshot_one["worker_selection"]["selected_worker_profile_version"]
                == TERRA_PROFILE_VERSION_BY_RECIPE_VERSION[
                    snapshot_one["recipe"]["recipe_version"]
                ]
                and snapshot_one["pre_transfer_estimate"]["worker_runtime"]
                ["reasoning_effort"]
                == TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[
                    TERRA_PROFILE_VERSION_BY_RECIPE_VERSION[
                        snapshot_one["recipe"]["recipe_version"]
                    ]
                ]
            )
            if snapshot_one["recipe"]["recipe_version"]
            in {
                HISTORICAL_RECIPE_VERSION + 2,
                HISTORICAL_RECIPE_VERSION + 3,
                HISTORICAL_RECIPE_VERSION + 4,
                HISTORICAL_RECIPE_VERSION + 5,
            }
            else True
        ),
        "one_call_ceiling": (
            snapshot_one["pre_transfer_estimate"]["planned_live_provider_calls"]
            == PLANNED_LIVE_PROVIDER_CALLS
            == 1
            and snapshot_one["pre_transfer_estimate"]["no_paid_retry"] is True
        ),
        "terra_request_configuration_preflight": (
            snapshot_one["pre_transfer_estimate"]["provider_request_configuration"]
            == (
                {
                    "api": "chat_completions",
                    "capability_profile": "openai_chat_gpt56_v1",
                    "model": "gpt-5.6-terra",
                    "response_format": "json_schema_strict",
                    "max_output_parameter": "max_completion_tokens",
                    "max_output_tokens": (
                        TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[
                            TERRA_PROFILE_VERSION_BY_RECIPE_VERSION[
                                snapshot_one["recipe"]["recipe_version"]
                            ]
                        ]
                    ),
                    "reasoning_effort": (
                        TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[
                            TERRA_PROFILE_VERSION_BY_RECIPE_VERSION[
                                snapshot_one["recipe"]["recipe_version"]
                            ]
                        ]
                    ),
                    "temperature": None,
                    "top_p": None,
                    "usage_shape": "chat_completions_usage_v1",
                    "provider_access_status": "provider_access_not_yet_proven",
                }
                if snapshot_one["recipe"]["recipe_version"]
                in {
                    HISTORICAL_RECIPE_VERSION + 2,
                    HISTORICAL_RECIPE_VERSION + 3,
                    HISTORICAL_RECIPE_VERSION + 4,
                }
                else {
                    "api": "responses",
                    "capability_profile": "openai_responses_gpt56_v1",
                    "model": "gpt-5.6-terra",
                    "response_format": "json_schema_strict",
                    "max_output_parameter": "max_output_tokens",
                    "max_output_tokens": (
                        TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION[
                            TERRA_PROFILE_VERSION_BY_RECIPE_VERSION[
                                snapshot_one["recipe"]["recipe_version"]
                            ]
                        ]
                    ),
                    "reasoning_effort": (
                        TERRA_REASONING_EFFORT_BY_PROFILE_VERSION[
                            TERRA_PROFILE_VERSION_BY_RECIPE_VERSION[
                                snapshot_one["recipe"]["recipe_version"]
                            ]
                        ]
                    ),
                    "temperature": None,
                    "top_p": None,
                    "usage_shape": "responses_usage_v1",
                    "provider_access_status": "provider_access_not_yet_proven",
                }
            )
            if snapshot_one["recipe"]["recipe_version"]
            in {
                HISTORICAL_RECIPE_VERSION + 2,
                HISTORICAL_RECIPE_VERSION + 3,
                HISTORICAL_RECIPE_VERSION + 4,
                HISTORICAL_RECIPE_VERSION + 5,
            }
            else True
        ),
        "patch_retained_bound_to_attempt": (
            snapshot_one["patch_retention"]["retained"] is not None
            and not snapshot_one["patch_retention"]["retained"].get("retention_error")
            and snapshot_one["worker_attempts"][0]["retained_artifact"] is not None
            and snapshot_one["worker_attempts"][0]["retained_artifact"]["sha256"]
            == snapshot_one["worker_attempts"][0]["patch_sha256"]
            and (Path(outcome.root) / "holdout"
                 / snapshot_one["patch_retention"]["retained"]["evidence_path"]).is_file()
        ),
        "oracle_workers_excluded_from_acceptance": (
            not any(
                classify_registry_worker(worker_id)["classification"]
                == WORKER_CLASS_ORACLE
                for worker_id in snapshot_one["worker_selection"]["recipe_ladder"]
            )
            and not any(
                item.get("classification") == WORKER_CLASS_ORACLE
                for item in snapshot_one["worker_selection"]["skipped"]
                if item["worker_id"] == snapshot_one["worker_selection"]["selected_worker_id"]
            )
        ),
        "holdout_definition_immutable": (
            snapshot_one["holdout_product_spec_sha256"] == PREDECLARED_HOLDOUT_PRODUCT_SHA256
            and snapshot_one["fixture_hash"] == FROZEN_FIXTURE_TREE_HASH
        ),
        "reference_solution_isolated_from_worker": all(
            item.get("reference_solution_leakage_detected") is False
            for item in snapshot_one["context_manifests"]
            if item.get("station_role") == "builder"
        )
        and any(
            item.get("station_role") == "builder"
            for item in snapshot_one["context_manifests"]
        ),
        **identity_checks,
    }


__all__ = [
    "ACCEPTED_PRODUCT_CLASS_ID",
    "ACCEPTED_SOURCE_FACTORY_RUN_ID",
    "HISTORICAL_RECIPE_ID",
    "HISTORICAL_RECIPE_SEMANTIC_HASH",
    "HISTORICAL_RECIPE_VERSION",
    "HOLDOUT_SELECTION_POLICY_ID",
    "HoldoutError",
    "HoldoutRetryBlocked",
    "HoldoutSelection",
    "HoldoutSiblingDefinition",
    "HoldoutTransferOutcome",
    "OWNER_PAIRED_002_FAILED_WORKER",
    "OWNER_PAIRED_002_PAIR_ID",
    "PLANNED_LIVE_PROVIDER_CALLS",
    "ACCEPTANCE_GENUINE_TRANSFER",
    "ACCEPTANCE_HARNESS_REHEARSAL",
    "ALTERNATE_GENUINE_WORKER_ID",
    "ALTERNATE_GENUINE_HOLDOUT_FAILURE",
    "ALTERNATE_WORKER_REVALIDATION_POLICY_ID",
    "GPT56_TERRA_WORKER_ID",
    "GPT56_TERRA_WORKER_VERSION",
    "GPT56_TERRA_REVALIDATION_POLICY_ID",
    "TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION",
    "TERRA_REASONING_EFFORT_BY_PROFILE_VERSION",
    "TERRA_PROFILE_VERSION_BY_RECIPE_VERSION",
    "COMPLETION_BUDGET_REVALIDATION_POLICY_ID",
    "REASONING_REVALIDATION_POLICY_ID",
    "EXECUTION_STRATEGY_REVALIDATION_POLICY_ID",
    "HISTORICAL_TERRA_TRUNCATED_RUN",
    "HISTORICAL_TERRA_SATURATION_RUNS",
    "HISTORICAL_TERRA_LOW_SATURATION_RUN",
    "HISTORICAL_TERRA_EXECUTION_STRATEGY_AUDIT",
    "REASONING_REVALIDATION_METADATA",
    "RESPONSES_STRATEGY_REVALIDATION_METADATA",
    "REPEATED_WORKER_FAILURE_AUDIT",
    "FROZEN_BUILDER_RENDERED_HASH",
    "FROZEN_FIXTURE_TREE_HASH",
    "GENUINE_HOLDOUT_FAILURE",
    "create_alternate_worker_v3",
    "create_gpt56_terra_worker_v4",
    "create_completion_budget_revalidation_v5",
    "create_reasoning_revalidation_v6",
    "create_responses_strategy_revalidation_v7",
    "classify_holdout_failure",
    "derive_builder_patch_output_contract",
    "gpt56_terra_holdout_worker_profile",
    "load_genuine_holdout_failure_record",
    "live_candidate_profiles",
    "extended_worker_registry",
    "PREDECLARED_HOLDOUT_PRODUCT_SHA256",
    "WARM_TRANSFER_POLICY_ID",
    "WORKER_CLASSIFICATION_POLICY_ID",
    "WORKER_CLASS_GENUINE",
    "WORKER_CLASS_INELIGIBLE",
    "WORKER_CLASS_ORACLE",
    "WORKER_CLASS_TEST_ONLY",
    "WORKER_SELECTION_POLICY_ID",
    "audit_worker_registry",
    "classify_registry_worker",
    "audit_holdout_transfer",
    "execute_holdout_warm_transfer",
    "holdout_candidate_catalog",
    "holdout_difference_summary",
    "load_owner_paired_002_record",
    "pre_transfer_estimate",
    "prepare_holdout_recipe",
    "prior_sibling_registry",
    "recipe_v2_fallback_audit",
    "replay_holdout",
    "replay_holdout_result",
    "select_holdout_sibling",
    "select_holdout_worker",
]
