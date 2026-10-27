"""REDI-08C14 Responses execution-strategy revalidation regressions.

The ONLY material execution change authorized by C14 is the API/runtime
strategy chat_completions -> responses on a new immutable WorkerProfile
version, bound to a new SHADOW recipe version at the UNCHANGED low reasoning
effort and 2048 max_output_tokens ceiling.  Everything else — provider, model,
holdout, task semantics, output contract, gates, one-call policy — must remain
frozen, and all three historical Chat Completions saturation runs must keep
reconstructing exactly their recorded configurations.  Zero network: every
provider interaction in these tests is a deterministic fake.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from redi_lab.cold_start import run_controlled_cold_start
from redi_lab.holdout_transfer import (
    ACCEPTED_PRODUCT_CLASS_ID,
    ACCEPTED_SOURCE_FACTORY_RUN_ID,
    ALTERNATE_GENUINE_HOLDOUT_FAILURE,
    ALTERNATE_GENUINE_WORKER_ID,
    BUILDER_PATCH_OUTPUT_POLICY_ID,
    EXECUTION_STRATEGY_REVALIDATION_POLICY_ID,
    FROZEN_BUILDER_RENDERED_HASH,
    FROZEN_FIXTURE_TREE_HASH,
    GPT56_TERRA_WORKER_ID,
    GENUINE_HOLDOUT_FAILURE,
    HISTORICAL_RECIPE_VERSION,
    HISTORICAL_TERRA_EXECUTION_STRATEGY_AUDIT,
    HISTORICAL_TERRA_LOW_SATURATION_RUN,
    PLANNED_LIVE_PROVIDER_CALLS,
    PREDECLARED_HOLDOUT_PRODUCT_SHA256,
    RESPONSES_STRATEGY_REVALIDATION_METADATA,
    TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION,
    TERRA_PROFILE_VERSION_BY_RECIPE_VERSION,
    TERRA_REASONING_EFFORT_BY_PROFILE_VERSION,
    audit_holdout_transfer,
    create_alternate_worker_v3,
    create_completion_budget_revalidation_v5,
    create_gpt56_terra_worker_v4,
    create_reasoning_revalidation_v6,
    create_responses_strategy_revalidation_v7,
    classify_holdout_failure,
    execute_holdout_warm_transfer,
    gpt56_terra_holdout_worker_profile,
    load_genuine_holdout_failure_record,
    load_owner_paired_002_record,
    prepare_holdout_recipe,
    prior_sibling_registry,
    replay_holdout,
    select_holdout_sibling,
    select_holdout_worker,
    HoldoutError,
)
from redi_lab.domain import FailureCategory
from redi_lab.recipe_store import RecipeStore, recipe_semantic_payload
from redi_lab.runtime.contracts import PatchOutputContract
from redi_lab.warm_start import inventory_product_class


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"
RUN_A_1024 = ROOT / "build" / "redi08c8-owner-terra-20260921T164206Z"
RUN_A_1024_ID = "factory_run_f4aa3a32e4754bfa94210404c549ccd8"
RUN_B_2048 = ROOT / "build" / "redi08c10-owner-terra-20260921T181641Z"
RUN_B_2048_ID = "factory_run_7e022c35f18e4ee99f68c3a856bcd54d"
RUN_C_LOW_2048 = ROOT / "build" / "redi08c12-owner-terra-low-20260922T164535Z"
RUN_C_LOW_2048_ID = "factory_run_ea8ccfe83c79412fac481f4bf266c8f4"

FROZEN_CONTRACT_HASH = "012b881902d568257763311ddd3a340e44f176542525ebba14dd030961b85abc"
VALID_BUILDER_TEXT = json.dumps(
    {
        "patches": [
            {
                "rel_path": "depot_replenishment/normalize.py",
                "action": "write",
                "content": "def normalize_depot_code(depot: str, code: str) -> str:\n    return f'{depot}#{code}'\n",
            }
        ]
    }
)


def _contract() -> PatchOutputContract:
    return PatchOutputContract(
        policy_id=BUILDER_PATCH_OUTPUT_POLICY_ID,
        policy_version=1,
        expected_edit_count=1,
        expected_action="write",
        expected_path="depot_replenishment/normalize.py",
        content_required=True,
        extra_edits_forbidden=True,
        allowed_scope=("depot_replenishment",),
    )


def _completed_responses_envelope(text: str) -> dict:
    return {
        "id": "resp_fake_completed",
        "status": "completed",
        "incomplete_details": None,
        "error": None,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": 538,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2048,
            "output_tokens_details": {"reasoning_tokens": 2048},
            "total_tokens": 2586,
        },
    }


INCOMPLETE_RESPONSES_ENVELOPE = {
    "id": "resp_fake_incomplete",
    "status": "incomplete",
    "incomplete_details": {"reason": "max_output_tokens"},
    "error": None,
    "output": [],
    "usage": {
        "input_tokens": 538,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 2048,
        "output_tokens_details": {"reasoning_tokens": 2048},
        "total_tokens": 2586,
    },
}

REFUSAL_RESPONSES_ENVELOPE = {
    "id": "resp_fake_refusal",
    "status": "completed",
    "incomplete_details": None,
    "error": None,
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "refusal", "refusal": "I cannot assist with that."}],
        }
    ],
    "usage": {
        "input_tokens": 538,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 64,
        "output_tokens_details": {"reasoning_tokens": 32},
        "total_tokens": 634,
    },
}

FAILED_RESPONSES_ENVELOPE = {
    "id": "resp_fake_failed",
    "status": "failed",
    "incomplete_details": None,
    "error": {"code": "server_error", "message": "upstream failure"},
    "output": [],
    "usage": {
        "input_tokens": 538,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 538,
    },
}


@pytest.fixture(scope="module")
def c14(tmp_path_factory):
    """Recipe lineage v2 -> ... -> v7 plus the frozen holdout selection."""
    work = tmp_path_factory.mktemp("redi08c14")
    if HISTORICAL_LEDGER.exists():
        source_ledger, source_run = HISTORICAL_LEDGER, ACCEPTED_SOURCE_FACTORY_RUN_ID
    else:
        cold = run_controlled_cold_start(output_root=work / "source", known_recipe=None)
        source_ledger, source_run = cold.db_path, cold.factory_run_id
    product_class = inventory_product_class(ACCEPTED_PRODUCT_CLASS_ID)
    store = RecipeStore(work / "recipes.sqlite")
    owner = load_owner_paired_002_record()
    gpt41_failure = load_genuine_holdout_failure_record(ROOT / "build")
    v2 = prepare_holdout_recipe(
        store=store,
        source_ledger=source_ledger,
        source_factory_run_id=source_run,
        product_class=product_class,
    )
    v3 = create_alternate_worker_v3(
        store=store,
        stored_v2=v2,
        product_class=product_class,
        owner_paired_002_record=owner,
        genuine_failure_record=gpt41_failure,
        alternate_worker_id=ALTERNATE_GENUINE_WORKER_ID,
    )
    v4 = create_gpt56_terra_worker_v4(
        store=store,
        stored_v3=v3,
        product_class=product_class,
        gpt41_failure_record=gpt41_failure,
        gpt4o_mini_failure_record=ALTERNATE_GENUINE_HOLDOUT_FAILURE,
    )
    v5 = create_completion_budget_revalidation_v5(
        store=store, stored_v4=v4, product_class=product_class
    )
    v6 = create_reasoning_revalidation_v6(
        store=store, stored_v5=v5, product_class=product_class
    )
    v7 = create_responses_strategy_revalidation_v7(
        store=store, stored_v6=v6, product_class=product_class
    )
    selection = select_holdout_sibling(
        product_class=product_class,
        prior_used_registry=prior_sibling_registry(
            product_class_id=str(product_class.id), owner_paired_002_record=owner
        ),
    )
    return {
        "work": work,
        "store": store,
        "product_class": product_class,
        "owner": owner,
        "v6": v6,
        "v7": v7,
        "selection": selection,
    }


def _run_fake(c14, stored, name, *, fake_responses_result=None):
    selection = select_holdout_worker(stored=stored, owner_paired_002_record=c14["owner"])
    return execute_holdout_warm_transfer(
        output_root=c14["work"] / name,
        product_class=c14["product_class"],
        store=c14["store"],
        stored=stored,
        selection=c14["selection"],
        worker_selection=selection,
        owner_paired_002_record=c14["owner"],
        source_revision="redi08c14-unit",
        live=False,
        fake_responses_result=fake_responses_result,
    )


# ---------------------------------------------------------------------------
# WorkerProfile / runtime versioning
# ---------------------------------------------------------------------------


def test_worker_profile_v4_changes_only_execution_api():
    v3 = gpt56_terra_holdout_worker_profile(version=3)
    v4 = gpt56_terra_holdout_worker_profile(version=4)
    assert {k for k in v3 if v3[k] != v4[k]} == {"version", "runtime"}
    runtime_keys = set(v3["runtime"]) | set(v4["runtime"])
    assert {k for k in runtime_keys if v3["runtime"].get(k) != v4["runtime"].get(k)} == {
        "api",
        "api_capability_profile",
        "max_output_parameter",
        "usage_shape",
        "conversation_state",
    }
    # Everything the owner froze stays byte-identical.
    for key in ("adapter", "response_format", "max_output_tokens", "reasoning_effort", "temperature", "top_p"):
        assert v3["runtime"][key] == v4["runtime"][key]
    assert v4["runtime"]["api"] == "responses"
    assert v4["runtime"]["api_capability_profile"] == "openai_responses_gpt56_v1"
    assert v4["runtime"]["max_output_parameter"] == "max_output_tokens"
    assert v4["runtime"]["usage_shape"] == "responses_usage_v1"
    assert v4["runtime"]["conversation_state"] == "stateless_single_call"
    assert v4["runtime"]["reasoning_effort"] == "low"
    assert v4["runtime"]["max_output_tokens"] == 2048
    assert v4["model"] == "gpt-5.6-terra"
    assert v4["provider"] == "openai"
    assert v4["structured_output_required"] is True
    assert v4["classification"] == "GENUINE_EXECUTION_WORKER"
    with pytest.raises(HoldoutError):
        gpt56_terra_holdout_worker_profile(version=5)


def test_chat_profile_versions_v1_to_v3_stay_frozen():
    assert TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION == {1: 1024, 2: 2048, 3: 2048, 4: 2048}
    assert TERRA_REASONING_EFFORT_BY_PROFILE_VERSION == {1: "medium", 2: "medium", 3: "low", 4: "low"}
    expected = {1: ("medium", 1024), 2: ("medium", 2048), 3: ("low", 2048)}
    for version, (effort, budget) in expected.items():
        profile = gpt56_terra_holdout_worker_profile(version=version)
        runtime = profile["runtime"]
        assert runtime["api"] == "chat_completions"
        assert runtime["api_capability_profile"] == "openai_chat_gpt56_v1"
        assert runtime["max_output_parameter"] == "max_completion_tokens"
        assert runtime["usage_shape"] == "chat_completions_usage_v1"
        assert runtime["reasoning_effort"] == effort
        assert runtime["max_output_tokens"] == budget


def test_recipe_v7_is_api_only_shadow_child_of_v6(c14):
    v6, v7 = c14["v6"], c14["v7"]
    assert v6.recipe.version == HISTORICAL_RECIPE_VERSION + 4
    assert v7.recipe.version == HISTORICAL_RECIPE_VERSION + 5
    assert str(v7.recipe.id) == str(v6.recipe.id)
    assert v7.lifecycle_state.value == "shadow"
    assert v7.revalidation_state == "VALID_FOR_CONTROLLED_EXPERIMENT"
    assert v7.provenance["parent_recipe_version"] == v6.recipe.version
    # Worker tuple unchanged: same single Terra worker, no new worker.
    assert v7.recipe.worker_preferences == v6.recipe.worker_preferences
    assert v7.recipe.worker_fallbacks == v6.recipe.worker_fallbacks
    payload_v6 = recipe_semantic_payload(v6.recipe)
    payload_v7 = recipe_semantic_payload(v7.recipe)
    assert payload_v7.pop("version") == payload_v6.pop("version") + 1
    assert set(payload_v7) == set(payload_v6)
    assert set(payload_v7["metadata"]) - set(payload_v6["metadata"]) == {
        "responses_strategy_revalidation"
    }
    assert (
        payload_v7["metadata"]["responses_strategy_revalidation"]
        == RESPONSES_STRATEGY_REVALIDATION_METADATA
    )
    assert RESPONSES_STRATEGY_REVALIDATION_METADATA["prior_value"] == "chat_completions"
    assert RESPONSES_STRATEGY_REVALIDATION_METADATA["value"] == "responses"
    assert RESPONSES_STRATEGY_REVALIDATION_METADATA["reasoning_effort_unchanged"] == "low"
    assert RESPONSES_STRATEGY_REVALIDATION_METADATA["completion_budget_unchanged_tokens"] == 2048
    assert "reasoning tokens and visible output share the 2048 bound" in (
        RESPONSES_STRATEGY_REVALIDATION_METADATA["max_output_tokens_semantics"]
    )
    # Provenance records ALL THREE Chat Completions saturation runs + verdicts.
    evidence = v7.provenance["revalidation_evidence"]
    runs = evidence["historical_terra_chat_completions_runs"]
    assert runs["run_a_medium_1024"]["reasoning_tokens"] == 1024
    assert runs["run_b_medium_2048"]["reasoning_tokens"] == 2048
    assert runs["run_c_low_2048"] == HISTORICAL_TERRA_LOW_SATURATION_RUN
    assert runs["run_c_low_2048"]["execution_api"] == "chat_completions"
    assert runs["run_c_low_2048"]["factory_run_id"] == RUN_C_LOW_2048_ID
    for record in runs.values():
        assert record["finish_reason"] == "length"
        assert record["visible_answer_content"] == "none"
    assert evidence["execution_strategy_audit"] == HISTORICAL_TERRA_EXECUTION_STRATEGY_AUDIT
    assert (
        evidence["execution_strategy_audit"]["audit_verdict"]
        == "EXECUTION_STRATEGY_REVIEW_REQUIRED"
    )
    assert "No model-capability conclusion" in evidence["no_capability_claim"]
    assert evidence["automatic_paid_retry"] is False
    # Registration metadata pins the exact API delta.
    assert v7.registration_metadata["revision_kind"] == "execution_api_only"
    assert v7.registration_metadata["authorized_worker_profile_version"] == 4
    assert v7.registration_metadata["authorized_execution_api"] == "responses"
    assert v7.registration_metadata["prior_execution_api"] == "chat_completions"
    assert v7.registration_metadata["authorized_reasoning_effort"] == "low"
    assert v7.registration_metadata["authorized_completion_budget"] == 2048
    assert v7.registration_metadata["automatic_paid_retry"] is False
    assert v7.provenance["revalidation_policy_id"] == EXECUTION_STRATEGY_REVALIDATION_POLICY_ID


def test_selection_matrix_resolves_all_four_frozen_configurations(c14):
    store = c14["store"]
    owner = c14["owner"]
    stored = {
        version: store.read(str(c14["v7"].recipe.id), version) for version in (4, 5, 6, 7)
    }
    expected = {
        HISTORICAL_RECIPE_VERSION + 2: (1, "chat_completions", "medium", 1024),
        HISTORICAL_RECIPE_VERSION + 3: (2, "chat_completions", "medium", 2048),
        HISTORICAL_RECIPE_VERSION + 4: (3, "chat_completions", "low", 2048),
        HISTORICAL_RECIPE_VERSION + 5: (4, "responses", "low", 2048),
    }
    for version, (profile_version, api, effort, budget) in expected.items():
        selection = select_holdout_worker(stored=stored[version], owner_paired_002_record=owner)
        assert selection["selected_worker_id"] == GPT56_TERRA_WORKER_ID
        assert selection["selected_worker_profile_version"] == profile_version
        runtime = selection["provider_binding"]["runtime_request_configuration"]
        assert runtime["api"] == api
        assert runtime["reasoning_effort"] == effort
        assert runtime["max_output_tokens"] == budget


# ---------------------------------------------------------------------------
# Capability profile and Responses request rendering
# ---------------------------------------------------------------------------


def test_responses_capability_profile_request_configuration():
    from redi_lab.runtime.live_provider_adapter import (
        LiveProviderBlockedError,
        LiveProviderConfig,
        OPENAI_RESPONSES_GPT56_V1,
    )

    config = LiveProviderConfig(
        env_key_name="REDI_OPENAI_API_KEY",
        model="gpt-5.6-terra",
        max_output_tokens=2048,
        reasoning_effort="low",
        temperature=None,
        top_p=None,
        api_capability_profile="openai_responses_gpt56_v1",
        provider_name="openai",
        api="responses",
    )
    assert config.capability_profile() is OPENAI_RESPONSES_GPT56_V1
    assert config.request_configuration_evidence() == {
        "api": "responses",
        "capability_profile": "openai_responses_gpt56_v1",
        "model": "gpt-5.6-terra",
        "response_format": "json_schema_strict",
        "max_output_parameter": "max_output_tokens",
        "max_output_tokens": 2048,
        "reasoning_effort": "low",
        "temperature": None,
        "top_p": None,
        "usage_shape": "responses_usage_v1",
        "provider_access_status": "provider_access_not_yet_proven",
    }
    # Fail-closed: unsupported sampling controls and unknown profiles/models.
    with pytest.raises(LiveProviderBlockedError):
        LiveProviderConfig(
            model="gpt-5.6-terra",
            temperature=0.0,
            api_capability_profile="openai_responses_gpt56_v1",
            api="responses",
        ).request_configuration_evidence()
    with pytest.raises(LiveProviderBlockedError):
        LiveProviderConfig(
            model="gpt-4o-mini",
            temperature=None,
            api_capability_profile="openai_responses_gpt56_v1",
            api="responses",
        ).capability_profile()
    with pytest.raises(LiveProviderBlockedError):
        LiveProviderConfig(
            model="gpt-5.6-terra",
            temperature=None,
            api_capability_profile="openai_responses_unknown",
            api="responses",
        ).capability_profile()
    with pytest.raises(LiveProviderBlockedError):
        LiveProviderConfig(
            model="gpt-5.6-terra",
            temperature=None,
            api="streaming",
        ).capability_profile()


def test_render_responses_request_uses_native_semantics_with_frozen_task_hash():
    from redi_lab.runtime.live_provider_adapter import (
        LIVE_STATION_SYSTEM_PROMPT,
        render_live_request,
        render_responses_request,
    )

    contract = _contract()
    chat = render_live_request(
        role="builder",
        task_id="task_x",
        instructions="INSTRUCTIONS",
        context_body="CONTEXT",
        patch_output_contract=contract,
    )
    rendered = render_responses_request(
        role="builder",
        task_id="task_x",
        instructions="INSTRUCTIONS",
        context_body="CONTEXT",
        patch_output_contract=contract,
        model="gpt-5.6-terra",
        reasoning_effort="low",
        max_output_tokens=2048,
    )
    kwargs = rendered["create_kwargs"]
    assert kwargs["model"] == "gpt-5.6-terra"
    assert kwargs["instructions"] == LIVE_STATION_SYSTEM_PROMPT == chat["system_prompt"]
    assert kwargs["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": chat["user_content"]}]}
    ]
    assert kwargs["max_output_tokens"] == 2048
    assert kwargs["reasoning"] == {"effort": "low"}
    assert kwargs["store"] is False
    assert kwargs["text"]["format"]["type"] == "json_schema"
    assert kwargs["text"]["format"]["name"] == "RediBuilderOutput"
    assert kwargs["text"]["format"]["strict"] is True
    # Same canonical patch contract schema as the Chat Completions envelope.
    chat_schema = chat["response_format"]["json_schema"]
    assert kwargs["text"]["format"]["schema"] == chat_schema["schema"]
    assert kwargs["text"]["format"]["strict"] == chat_schema["strict"]
    # No forbidden capabilities ride along.
    for forbidden in ("tools", "previous_response_id", "background", "temperature", "top_p"):
        assert forbidden not in kwargs
    # Same task semantics, different execution API envelope.
    assert rendered["task_semantic_context_hash"] == chat["task_semantic_context_hash"]
    assert rendered["responses_request_hash"] != rendered["chat_full_request_hash"]
    # Deterministic hash.
    again = render_responses_request(
        role="builder",
        task_id="task_x",
        instructions="INSTRUCTIONS",
        context_body="CONTEXT",
        patch_output_contract=contract,
        model="gpt-5.6-terra",
        reasoning_effort="low",
        max_output_tokens=2048,
    )
    assert again["responses_request_hash"] == rendered["responses_request_hash"]


# ---------------------------------------------------------------------------
# Responses result normalization + deterministic failure taxonomy
# ---------------------------------------------------------------------------


def test_normalize_responses_result_completed_valid():
    from redi_lab.runtime.live_provider_adapter import normalize_responses_result

    normalized = normalize_responses_result(
        _completed_responses_envelope(VALID_BUILDER_TEXT), elapsed_seconds=1.25
    )
    assert normalized["output_text"] == VALID_BUILDER_TEXT
    pc = normalized["provider_completion"]
    assert pc["api"] == "responses"
    assert pc["status"] == "completed"
    assert pc["finish_reason"] == "stop"
    assert pc["incomplete"] is False
    assert pc["refusal_present"] is False
    assert pc["provider_error"] is False
    assert pc["visible_output_present"] is True
    assert pc["output_text_bytes"] == len(VALID_BUILDER_TEXT.encode("utf-8"))
    assert pc["completion_token_details"] == {"reasoning_tokens": 2048}
    assert normalized["tokens"] == {
        "input": 538,
        "output": 2048,
        "cached_input": 0,
        "reasoning": 2048,
        "total": 2586,
    }


def test_normalize_responses_result_incomplete_truncation_semantics():
    from redi_lab.runtime.live_provider_adapter import normalize_responses_result

    normalized = normalize_responses_result(
        INCOMPLETE_RESPONSES_ENVELOPE, elapsed_seconds=2.0
    )
    pc = normalized["provider_completion"]
    assert pc["status"] == "incomplete"
    assert pc["incomplete_details_reason"] == "max_output_tokens"
    # Chat Completions parity: max_output_tokens truncation maps to "length".
    assert pc["finish_reason"] == "length"
    assert pc["incomplete"] is True
    assert pc["visible_output_present"] is False
    assert normalized["output_text"] == ""
    assert pc["output_text_sha256"] is None
    assert normalized["tokens"]["reasoning"] == 2048
    assert normalized["tokens"]["output"] == 2048


def test_normalize_responses_result_refusal_failed_and_zero_visible():
    from redi_lab.runtime.live_provider_adapter import normalize_responses_result

    refusal = normalize_responses_result(REFUSAL_RESPONSES_ENVELOPE, elapsed_seconds=0.5)
    assert refusal["provider_completion"]["refusal_present"] is True
    assert refusal["provider_completion"]["incomplete"] is True
    assert refusal["provider_completion"]["visible_output_present"] is False

    failed = normalize_responses_result(FAILED_RESPONSES_ENVELOPE, elapsed_seconds=0.5)
    pc = failed["provider_completion"]
    assert pc["status"] == "failed"
    assert pc["finish_reason"] is None
    assert pc["error_code"] == "server_error"
    assert pc["provider_error"] is True
    assert pc["incomplete"] is False

    zero = normalize_responses_result(
        _completed_responses_envelope(""), elapsed_seconds=0.5
    )
    assert zero["provider_completion"]["visible_output_present"] is False
    assert zero["provider_completion"]["finish_reason"] == "stop"


def test_failure_taxonomy_mapping_is_predeclared_and_deterministic():
    incomplete = {"finish_reason": "length", "incomplete": True, "provider_error": False}
    refusal = {"finish_reason": "stop", "incomplete": True, "refusal_present": True, "provider_error": False}
    provider_error = {"incomplete": False, "provider_error": True}
    schema_fail = {"incomplete": False, "provider_error": False}
    gates_schema_fail = {"structured_output_schema": "FAIL", "patch_output_contract": "NOT_RUN"}
    tool = FailureCategory.TOOL_FAILURE
    budget = FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION
    det = FailureCategory.DETERMINISTIC_TEST_FAILURE
    gates_clean = {"structured_output_schema": "PASS", "patch_output_contract": "PASS"}
    expected = [
        ({"retention_failed": True, "provider_completion": {}, "gate_outcomes": gates_clean, "provider_failed": None},
         ("EVIDENCE_RETENTION_FAILURE", tool)),
        ({"retention_failed": False, "provider_completion": incomplete, "gate_outcomes": gates_clean, "provider_failed": None},
         ("PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE", budget)),
        ({"retention_failed": False, "provider_completion": refusal, "gate_outcomes": gates_clean, "provider_failed": None},
         ("PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE", budget)),
        ({"retention_failed": False, "provider_completion": provider_error, "gate_outcomes": gates_clean, "provider_failed": "LiveProviderBlockedError: x"},
         ("PROVIDER_CALL_FAILURE", tool)),
        ({"retention_failed": False, "provider_completion": schema_fail, "gate_outcomes": gates_schema_fail, "provider_failed": None},
         ("STRUCTURED_OUTPUT_SCHEMA_FAILURE", det)),
    ]
    for kwargs, (classification, category) in expected:
        result = classify_holdout_failure(**kwargs)
        assert result == (classification, category), (kwargs, result)
    # Deterministic: same inputs, same outputs.
    repeat = classify_holdout_failure(
        provider_failed=None,
        provider_completion=incomplete,
        gate_outcomes=gates_clean,
        retention_failed=False,
    )
    assert repeat == ("PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE", FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION)


# ---------------------------------------------------------------------------
# Adapter parity: Chat Completions vs Responses, no network
# ---------------------------------------------------------------------------


class _FakeChatCompletions:
    def __init__(self, response, sink):
        self._response = response
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return self._response


class _FakeResponses:
    def __init__(self, response, sink):
        self._response = response
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return self._response


def _chat_completion(text):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, refusal=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=538,
            completion_tokens=2048,
            total_tokens=2586,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            completion_tokens_details=SimpleNamespace(
                reasoning_tokens=2048,
                accepted_prediction_tokens=0,
                rejected_prediction_tokens=0,
                audio_tokens=0,
            ),
        ),
        model="gpt-5.6-terra",
    )


def _responses_result(text):
    return SimpleNamespace(
        id="resp_fake_parity",
        status="completed",
        incomplete_details=None,
        error=None,
        model="gpt-5.6-terra",
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        usage=SimpleNamespace(
            input_tokens=538,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
            output_tokens=2048,
            output_tokens_details=SimpleNamespace(reasoning_tokens=2048),
            total_tokens=2586,
        ),
    )


def _run_adapter(monkeypatch, *, api, fake_response):
    import os

    import openai

    from redi_lab.runtime.live_provider_adapter import LiveProviderAdapter, LiveProviderConfig

    sink: list[dict] = []
    if api == "chat_completions":
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeChatCompletions(fake_response, sink))
        )
        config = LiveProviderConfig(
            model="gpt-5.6-terra",
            max_output_tokens=2048,
            reasoning_effort="low",
            temperature=None,
            top_p=None,
            api_capability_profile="openai_chat_gpt56_v1",
            api="chat_completions",
        )
    else:
        client = SimpleNamespace(responses=_FakeResponses(fake_response, sink))
        config = LiveProviderConfig(
            model="gpt-5.6-terra",
            max_output_tokens=2048,
            reasoning_effort="low",
            temperature=None,
            top_p=None,
            api_capability_profile="openai_responses_gpt56_v1",
            api="responses",
        )
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: client)
    monkeypatch.setenv("REDI_OPENAI_API_KEY", "test-key-redaction-check")
    adapter = LiveProviderAdapter(config=config)
    result = adapter.run_node(
        task_id="task_parity",
        station_id="station_impl",
        worker_id=GPT56_TERRA_WORKER_ID,
        context={
            "role": "builder",
            "instructions": "INSTRUCTIONS",
            "context_body": "CONTEXT",
            "patch_output_contract": _contract(),
        },
        workspace_root=None,
        station_role="builder",
    )
    return result, sink


def test_adapter_parity_chat_vs_responses_same_pipeline(monkeypatch):
    chat_result, chat_sink = _run_adapter(
        monkeypatch, api="chat_completions", fake_response=_chat_completion(VALID_BUILDER_TEXT)
    )
    responses_result, responses_sink = _run_adapter(
        monkeypatch, api="responses", fake_response=_responses_result(VALID_BUILDER_TEXT)
    )
    # Identical RediBuilderOutput / PatchEdit / shape / validation outcome.
    assert chat_result.error_message is None
    assert responses_result.error_message is None
    assert [p.to_dict() if hasattr(p, "to_dict") else p for p in chat_result.patches] == [
        p.to_dict() if hasattr(p, "to_dict") else p for p in responses_result.patches
    ]
    assert len(responses_result.patches) == 1
    assert responses_result.patches[0].rel_path == "depot_replenishment/normalize.py"
    assert responses_result.patches[0].action == "write"
    assert chat_result.output_shape["structured_output_validated"] is True
    assert (
        chat_result.output_shape["schema_identity"]["policy_hash"]
        == responses_result.output_shape["schema_identity"]["policy_hash"]
        == FROZEN_CONTRACT_HASH
    )
    assert chat_result.observed_patches[0].safe_shape() == responses_result.observed_patches[0].safe_shape()
    # Equivalent normalized telemetry concepts.
    chat_pc = chat_result.provider_completion
    responses_pc = responses_result.provider_completion
    assert chat_pc["finish_reason"] == responses_pc["finish_reason"] == "stop"
    assert chat_pc["incomplete"] is responses_pc["incomplete"] is False
    assert chat_pc["refusal_present"] is responses_pc["refusal_present"] is False
    assert chat_pc["completion_token_details"]["reasoning_tokens"] == 2048
    assert responses_pc["completion_token_details"]["reasoning_tokens"] == 2048
    assert chat_result.token_usage.input == responses_result.token_usage.input == 538
    assert chat_result.token_usage.output == responses_result.token_usage.output == 2048
    assert responses_result.token_usage.reasoning == 2048
    # API-native request semantics on the wire.
    assert chat_sink[0]["max_completion_tokens"] == 2048
    assert chat_sink[0]["reasoning_effort"] == "low"
    assert responses_sink[0]["max_output_tokens"] == 2048
    assert responses_sink[0]["reasoning"] == {"effort": "low"}
    assert responses_sink[0]["store"] is False
    assert (
        chat_sink[0]["response_format"]["json_schema"]["schema"]
        == responses_sink[0]["text"]["format"]["schema"]
    )
    assert "previous_response_id" not in responses_sink[0]
    assert "tools" not in responses_sink[0]


def test_responses_adapter_surfaces_provider_error_as_typed_failure(monkeypatch):
    result, _ = _run_adapter(
        monkeypatch, api="responses", fake_response=_responses_result("not json {")
    )
    assert result.error_message == (
        "STRUCTURED_OUTPUT_SCHEMA_FAILURE role=builder category=JSONDecodeError"
    )
    assert result.patches == ()


# ---------------------------------------------------------------------------
# Dry rehearsals through the exact holdout path
# ---------------------------------------------------------------------------


def test_v7_dry_rehearsal_success_9_gates_replay_x2(c14):
    outcome = _run_fake(c14, c14["v7"], "v7_rehearsal_success")
    one = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    two = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert outcome.provider_calls == outcome.network_calls == 0
    assert one == two
    assert outcome.verified is True
    assert one["acceptance_semantics"] == "HARNESS_REHEARSAL_ONLY"
    assert one["counts_as_transfer_evidence"] is False
    assert len(outcome.gate_outcomes) == 9
    assert all(state == "PASS" for state in outcome.gate_outcomes.values())
    # Holdout + context freeze.
    assert one["holdout_product_spec_sha256"] == PREDECLARED_HOLDOUT_PRODUCT_SHA256
    assert one["fixture_hash"] == FROZEN_FIXTURE_TREE_HASH
    builder = next(m for m in one["context_manifests"] if m["station_role"] == "builder")
    assert builder["rendered_hash"] == FROZEN_BUILDER_RENDERED_HASH
    # Output contract unchanged.
    assert one["builder_output_contract"]["canonical_hash"] == FROZEN_CONTRACT_HASH
    # The experimental configuration: responses / low / 2048, profile v4.
    estimate = one["pre_transfer_estimate"]
    assert estimate["worker_runtime"]["api"] == "responses"
    assert estimate["worker_runtime"]["reasoning_effort"] == "low"
    assert estimate["max_output_tokens"] == 2048
    assert estimate["planned_live_provider_calls"] == PLANNED_LIVE_PROVIDER_CALLS == 1
    assert estimate["no_paid_retry"] is True
    assert estimate["worker_profile"]["version"] == 4
    assert one["recipe"]["recipe_version"] == HISTORICAL_RECIPE_VERSION + 5
    assert one["revalidation_decision"]["decision"] == "V7_RESPONSES_STRATEGY_REVALIDATION_PINNED"
    # Responses telemetry + separate request hash prove same semantics/new API.
    assert "responses_request_hash" in one
    attempt_evidence = one["worker_attempts"][0]
    assert attempt_evidence["execution_kind"] == "governed_fake_adapter_slot"
    assert one["patch_retention"]["retained"] is not None
    # Strict self-audit fully green.
    audit = dict(outcome.audit) | audit_holdout_transfer(outcome=outcome, strict_identity=True)
    assert all(audit.values()), {k: v for k, v in audit.items() if not v}


def test_v7_dry_rehearsal_incomplete_maps_to_typed_provider_failure(c14):
    outcome = _run_fake(
        c14,
        c14["v7"],
        "v7_rehearsal_incomplete",
        fake_responses_result=INCOMPLETE_RESPONSES_ENVELOPE,
    )
    one = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    two = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert one == two
    assert outcome.provider_calls == outcome.network_calls == 0
    assert outcome.verified is False
    result = json.loads(outcome.result_path.read_text(encoding="utf-8"))
    # The failure is typed as provider-incomplete, NOT as a schema failure.
    assert result["failure_classification"] == "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE"
    pc = result["provider_completion"]
    assert pc["finish_reason"] == "length"
    assert pc["incomplete_details_reason"] == "max_output_tokens"
    assert pc["completion_token_details"]["reasoning_tokens"] == 2048
    assert pc["visible_output_present"] is False
    gates = {gate["gate_identity"]: gate["status"] for gate in one["gate_records"]}
    assert gates["structured_output_schema"] == "FAIL"
    downstream = {k: v for k, v in gates.items() if k != "structured_output_schema"}
    assert set(downstream.values()) == {"NOT_RUN"}
    # No erroneous patch artifact exists.
    assert result["patch_retention"]["retained"] is None
    assert result["patch_retention"]["retained_artifacts"] == []
    assert result["patch_retention"]["retention_failures"] == []
    assert outcome.gate_outcomes["structured_output_schema"] == "FAIL"


def test_chat_and_responses_dry_paths_converge_on_same_patch_bytes(c14):
    chat_outcome = _run_fake(c14, c14["v6"], "parity_chat")
    responses_outcome = _run_fake(c14, c14["v7"], "parity_responses")
    chat_snapshot = replay_holdout(
        db_path=chat_outcome.db_path, factory_run_id=chat_outcome.factory_run_id
    )
    responses_snapshot = replay_holdout(
        db_path=responses_outcome.db_path, factory_run_id=responses_outcome.factory_run_id
    )
    chat_attempt = chat_snapshot["worker_attempts"][0]
    responses_attempt = responses_snapshot["worker_attempts"][0]
    assert chat_attempt["patch_sha256"] == responses_attempt["patch_sha256"]
    assert (
        chat_attempt["retained_artifact"]["sha256"]
        == responses_attempt["retained_artifact"]["sha256"]
    )
    assert chat_outcome.gate_outcomes == responses_outcome.gate_outcomes
    # The responses rehearsal carries the API-strategy evidence the chat one does not.
    responses_result = json.loads(responses_outcome.result_path.read_text(encoding="utf-8"))
    chat_result = json.loads(chat_outcome.result_path.read_text(encoding="utf-8"))
    assert "responses_request_hash" in responses_result
    assert "responses_request_hash" not in chat_result
    assert responses_result["provider_completion"]["api"] == "responses"
    assert "api" not in chat_result["provider_completion"]


def test_fake_responses_result_blocked_for_live_mode(c14):
    worker_selection = select_holdout_worker(stored=c14["v7"], owner_paired_002_record=c14["owner"])
    with pytest.raises(HoldoutError, match="dry rehearsal"):
        execute_holdout_warm_transfer(
            output_root=c14["work"] / "live_guard",
            product_class=c14["product_class"],
            store=c14["store"],
            stored=c14["v7"],
            selection=c14["selection"],
            worker_selection=worker_selection,
            owner_paired_002_record=c14["owner"],
            source_revision="redi08c14-unit",
            live=True,
            fake_responses_result=INCOMPLETE_RESPONSES_ENVELOPE,
        )


# ---------------------------------------------------------------------------
# Historical replay + freeze guarantees
# ---------------------------------------------------------------------------


def test_one_call_policy_and_worker_freeze_unchanged(c14):
    v7 = c14["v7"]
    assert PLANNED_LIVE_PROVIDER_CALLS == 1
    assert v7.registration_metadata["automatic_paid_retry"] is False
    implementation_station = next(iter(v7.recipe.worker_preferences))
    assert list(v7.recipe.worker_preferences[implementation_station]) == [GPT56_TERRA_WORKER_ID]
    assert list(v7.recipe.worker_fallbacks[implementation_station]) == [GPT56_TERRA_WORKER_ID]
    versions = {
        stored.recipe.version: stored.lifecycle_state.value
        for stored in c14["store"].list_versions(str(v7.recipe.id))
    }
    assert set(versions) == {1, 2, 3, 4, 5, 6, 7}
    assert versions[1] == "candidate"
    assert all(state == "shadow" for version, state in versions.items() if version >= 2)


@pytest.mark.skipif(
    not all(
        (path / "run" / "holdout" / "evidence.sqlite").exists()
        for path in (RUN_A_1024, RUN_B_2048, RUN_C_LOW_2048)
    ),
    reason="historical Terra run artifacts not present on this machine",
)
def test_historical_replay_matrix_stays_chat_completions():
    expected = {
        RUN_A_1024: (RUN_A_1024_ID, "medium", 1024, 1024),
        RUN_B_2048: (RUN_B_2048_ID, "medium", 2048, 2048),
        RUN_C_LOW_2048: (RUN_C_LOW_2048_ID, "low", 2048, 2048),
    }
    for run_root, (run_id, effort, budget, reasoning) in expected.items():
        snapshot = replay_holdout(
            db_path=run_root / "run" / "holdout" / "evidence.sqlite", factory_run_id=run_id
        )
        runtime = snapshot["pre_transfer_estimate"]["worker_runtime"]
        assert runtime["api"] == "chat_completions"
        assert runtime["api_capability_profile"] == "openai_chat_gpt56_v1"
        assert runtime["max_output_parameter"] == "max_completion_tokens"
        assert runtime["reasoning_effort"] == effort
        assert runtime["max_output_tokens"] == budget
        assert (
            snapshot["provider_completion"]["completion_token_details"]["reasoning_tokens"]
            == reasoning
        )


@pytest.mark.skipif(
    not all(
        (path / "run" / "holdout" / "evidence.sqlite").exists()
        for path in (RUN_A_1024, RUN_B_2048, RUN_C_LOW_2048)
    ),
    reason="historical Terra run artifacts not present on this machine",
)
def test_registry_drift_does_not_rewrite_history(c14, monkeypatch):
    import redi_lab.holdout_transfer as ht

    outcome = _run_fake(c14, c14["v7"], "v7_replay_independence")
    monkeypatch.setattr(
        ht, "TERRA_COMPLETION_BUDGET_BY_PROFILE_VERSION", {1: 7777, 2: 7777, 3: 7777, 4: 7777}
    )
    monkeypatch.setattr(
        ht,
        "TERRA_REASONING_EFFORT_BY_PROFILE_VERSION",
        {1: "high", 2: "high", 3: "xhigh", 4: "xhigh"},
    )
    monkeypatch.setattr(ht, "TERRA_PROFILE_VERSION_BY_RECIPE_VERSION", {})
    expected = {
        RUN_A_1024: (RUN_A_1024_ID, "medium", 1024),
        RUN_B_2048: (RUN_B_2048_ID, "medium", 2048),
        RUN_C_LOW_2048: (RUN_C_LOW_2048_ID, "low", 2048),
    }
    for run_root, (run_id, effort, budget) in expected.items():
        snapshot = replay_holdout(
            db_path=run_root / "run" / "holdout" / "evidence.sqlite", factory_run_id=run_id
        )
        assert snapshot["pre_transfer_estimate"]["worker_runtime"]["reasoning_effort"] == effort
        assert snapshot["pre_transfer_estimate"]["worker_runtime"]["max_output_tokens"] == budget
        assert snapshot["pre_transfer_estimate"]["worker_runtime"]["api"] == "chat_completions"
    replayed = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert replayed["pre_transfer_estimate"]["worker_runtime"]["api"] == "responses"
    assert replayed["pre_transfer_estimate"]["worker_runtime"]["reasoning_effort"] == "low"
    assert replayed["pre_transfer_estimate"]["worker_runtime"]["max_output_tokens"] == 2048
