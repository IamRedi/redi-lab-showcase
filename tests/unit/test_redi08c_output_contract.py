"""REDI-08C8 builder output-contract and failed-output forensic regressions."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from redi_lab.cold_start import run_controlled_cold_start
from redi_lab.evidence.artifacts import ArtifactRetentionError, verify_retained_artifact
from redi_lab.holdout_transfer import (
    ACCEPTED_PRODUCT_CLASS_ID,
    ACCEPTED_SOURCE_FACTORY_RUN_ID,
    ALTERNATE_GENUINE_HOLDOUT_FAILURE,
    ALTERNATE_GENUINE_WORKER_ID,
    BUILDER_PATCH_OUTPUT_POLICY_ID,
    FROZEN_BUILDER_RENDERED_HASH,
    GPT56_TERRA_WORKER_ID,
    GENUINE_HOLDOUT_FAILURE,
    create_alternate_worker_v3,
    create_gpt56_terra_worker_v4,
    execute_holdout_warm_transfer,
    load_genuine_holdout_failure_record,
    load_owner_paired_002_record,
    prepare_holdout_recipe,
    prior_sibling_registry,
    replay_holdout,
    select_holdout_sibling,
    select_holdout_worker,
)
from redi_lab.recipe_store import RecipeStore
from redi_lab.domain import StationId, TaskId, WorkerId
from redi_lab.runtime.contracts import PatchOutputContract
from redi_lab.runtime.live_provider_adapter import (
    OPENAI_CHAT_GPT56_V1,
    LiveProviderAdapter,
    LiveProviderConfig,
    parse_builder_output_object,
    render_live_request,
)
from redi_lab.warm_start import inventory_product_class


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_LEDGER = ROOT / "artifacts" / "redi07b" / "redi07b-live-3962bb5a22bc" / "evidence.sqlite"


@pytest.fixture(scope="module")
def terra_v4(tmp_path_factory):
    """The exact Terra v4 profile, exercised exclusively through dry fakes."""
    work = tmp_path_factory.mktemp("redi08c8_terra")
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
        expected_semantic_hash=None if not HISTORICAL_LEDGER.exists() else None,
    )
    v3 = create_alternate_worker_v3(
        store=store,
        stored_v2=v2,
        product_class=product_class,
        owner_paired_002_record=owner,
        genuine_failure_record=gpt41_failure,
        alternate_worker_id=ALTERNATE_GENUINE_WORKER_ID,
    )
    stored = create_gpt56_terra_worker_v4(
        store=store,
        stored_v3=v3,
        product_class=product_class,
        gpt41_failure_record=gpt41_failure,
        gpt4o_mini_failure_record=ALTERNATE_GENUINE_HOLDOUT_FAILURE,
    )
    selection = select_holdout_sibling(
        product_class=product_class,
        prior_used_registry=prior_sibling_registry(
            product_class_id=str(product_class.id), owner_paired_002_record=owner
        ),
    )
    worker_selection = select_holdout_worker(
        stored=stored,
        owner_paired_002_record=owner,
        exclude_worker_ids={GENUINE_HOLDOUT_FAILURE["failed_worker"]: "already failed frozen holdout"},
        exclude_models={GENUINE_HOLDOUT_FAILURE["model"]: "same model already failed frozen holdout"},
    )
    assert worker_selection["selected_worker_id"] == GPT56_TERRA_WORKER_ID
    yield {
        "work": work,
        "store": store,
        "stored": stored,
        "selection": selection,
        "worker_selection": worker_selection,
        "owner": owner,
        "product_class": product_class,
    }
    store.close()


def _run_fake(terra_v4, name: str, fake_builder_output: dict | None):
    return execute_holdout_warm_transfer(
        output_root=terra_v4["work"] / name,
        product_class=terra_v4["product_class"],
        store=terra_v4["store"],
        stored=terra_v4["stored"],
        selection=terra_v4["selection"],
        worker_selection=terra_v4["worker_selection"],
        owner_paired_002_record=terra_v4["owner"],
        source_revision="redi08c8-unit",
        live=False,
        fake_builder_output=fake_builder_output,
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


def test_builder_contract_drives_instruction_schema_and_local_gate():
    contract = _contract()
    request = render_live_request(
        role="builder",
        task_id="implement_normalize",
        instructions="functional task stays frozen",
        context_body="public source only",
        patch_output_contract=contract,
    )
    schema = request["response_format"]["json_schema"]["schema"]
    patches_schema = schema["properties"]["patches"]
    item_schema = patches_schema["items"]
    assert "Return exactly 1 patch/edit." in request["user_content"]
    assert "depot_replenishment/normalize.py" in request["user_content"]
    assert patches_schema["minItems"] == patches_schema["maxItems"] == 1
    assert item_schema["properties"]["action"]["enum"] == ["write"]
    assert item_schema["properties"]["rel_path"]["enum"] == [contract.expected_path]
    assert item_schema["properties"]["content"] == {"type": "string"}
    parsed = parse_builder_output_object(
        {"patches": [{"rel_path": contract.expected_path, "action": "write", "content": ""}]},
        patch_output_contract=contract,
    )
    assert parsed["safe_error"] is None
    assert contract.validate_patches(parsed["patches"]) is None
    assert request["task_semantic_context_hash"] != request["full_request_hash"]


def test_provider_completion_metadata_is_safe_and_marks_incomplete(monkeypatch, tmp_path):
    contract = _contract()
    response = SimpleNamespace(
        model="gpt-5.6-terra",
        choices=[SimpleNamespace(
            finish_reason="length",
            message=SimpleNamespace(
                refusal=None,
                content=json.dumps({"patches": [{
                    "rel_path": contract.expected_path,
                    "action": "write",
                    "content": "x = 1\n",
                }]}),
            ),
        )],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=1024,
            total_tokens=1034,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=1010),
        ),
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: response))
    )
    fake_sdk = SimpleNamespace(OpenAI=lambda **_kwargs: fake_client)
    config = LiveProviderConfig(
        model="gpt-5.6-terra",
        max_output_tokens=1024,
        temperature=None,
        top_p=None,
        reasoning_effort="medium",
        api_capability_profile=OPENAI_CHAT_GPT56_V1.profile_id,
    )
    adapter = LiveProviderAdapter(config=config)
    monkeypatch.setenv("REDI_OPENAI_API_KEY", "unit-test-key")
    monkeypatch.setattr(adapter, "_require", lambda: (fake_sdk, "unit-test-key"))
    result = adapter.run_node(
        task_id=TaskId("implement_normalize"),
        station_id=StationId("station_warm_implementation"),
        worker_id=WorkerId(GPT56_TERRA_WORKER_ID),
        context={
            "role": "builder",
            "instructions": "functional task stays frozen",
            "context_body": "public source only",
            "patch_output_contract": contract,
        },
        workspace_root=tmp_path,
    )
    assert result.error_message is None
    assert result.provider_completion == {
        "finish_reason": "length",
        "refusal_present": False,
        "incomplete": True,
        "completion_token_details": {"reasoning_tokens": 1010},
        "provider_call_elapsed_seconds": result.provider_completion["provider_call_elapsed_seconds"],
    }
    assert result.token_usage and result.token_usage.output == 1024


@pytest.mark.parametrize(
    ("label", "payload", "reason", "count", "retained_count"),
    [
        ("zero", {"patches": []}, "patch_count_expected_1_got_0", 0, 0),
        ("two", {"patches": [
            {"rel_path": "depot_replenishment/normalize.py", "action": "write", "content": "a"},
            {"rel_path": "depot_replenishment/normalize.py", "action": "write", "content": "b"},
        ]}, "patch_count_expected_1_got_2", 2, 2),
        ("delete", {"patches": [
            {"rel_path": "depot_replenishment/normalize.py", "action": "delete"}
        ]}, "patch[0]_write_missing_content", 1, 0),
        ("wrong_path", {"patches": [
            {"rel_path": "../../escape.py", "action": "write", "content": "x = 1\n"}
        ]}, "patch_0_path_does_not_match_contract", 1, 1),
        ("missing_content", {"patches": [
            {"rel_path": "depot_replenishment/normalize.py", "action": "write"}
        ]}, "patch[0]_write_missing_content", 1, 0),
        ("null_content", {"patches": [
            {"rel_path": "depot_replenishment/normalize.py", "action": "write", "content": None}
        ]}, "patch[0]_write_content_not_string", 1, 0),
        ("extra_edit_key", {"patches": [
            {"rel_path": "depot_replenishment/normalize.py", "action": "write", "content": "x", "extra": "no"}
        ]}, "patch[0]_extra_key_extra", 1, 1),
    ],
)
def test_invalid_outputs_have_exact_shape_and_retention(terra_v4, label, payload, reason, count, retained_count):
    outcome = _run_fake(terra_v4, f"invalid_{label}", payload)
    snapshot = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert outcome.verified is False
    assert snapshot["failure_classification"] == "STRUCTURED_OUTPUT_SCHEMA_FAILURE"
    assert snapshot["parsed_builder_output"]["parsed_patch_count"] == count
    assert reason in snapshot["gate_records"][0]["reason"]
    assert len(snapshot["patch_retention"]["retained_artifacts"]) == retained_count
    assert snapshot["worker_attempts"][0]["output_shape"] == snapshot["parsed_builder_output"]
    for retained in snapshot["patch_retention"]["retained_artifacts"]:
        assert retained["evidence_path"].startswith("artifacts/handoffs/")
        assert ".." not in retained["evidence_path"]


def test_exact_terra_v4_valid_fake_rehearsal_and_replay(terra_v4):
    outcome = _run_fake(terra_v4, "valid", None)
    one = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    two = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    builder = next(item for item in one["context_manifests"] if item["station_role"] == "builder")
    profile = one["worker_selection"]["selected_worker_profile"]
    assert outcome.verified and outcome.provider_calls == outcome.network_calls == 0
    assert one == two
    assert one["acceptance_semantics"] == "HARNESS_REHEARSAL_ONLY"
    assert one["counts_as_transfer_evidence"] is False
    assert builder["rendered_hash"] == FROZEN_BUILDER_RENDERED_HASH
    assert builder["task_semantic_context_hash"] != builder["full_request_hash"]
    assert one["builder_output_contract"]["policy_id"] == BUILDER_PATCH_OUTPUT_POLICY_ID
    assert profile["id"] == GPT56_TERRA_WORKER_ID
    assert profile["model"] == "gpt-5.6-terra"
    assert profile["runtime"]["reasoning_effort"] == "medium"
    assert profile["runtime"]["max_output_tokens"] == 1024
    assert all(state == "PASS" for state in outcome.gate_outcomes.values())
    retained = one["patch_retention"]["retained"]
    assert retained is not None
    verified = verify_retained_artifact(
        run_artifact_root=outcome.root / "holdout" / "artifacts",
        retained_path=retained["retained_path"],
        artifact_id=retained["artifact_id"],
        expected_sha256=retained["sha256"],
        expected_size=retained["bytes"],
    )
    assert verified["sha256"] == retained["sha256"]


def test_functional_failure_retains_actual_patch_and_tamper_is_detected(terra_v4):
    payload = {"patches": [{
        "rel_path": "depot_replenishment/normalize.py",
        "action": "write",
        "content": "def normalize_depot_code(location, bin_code):\n    return 'wrong'\n",
    }]}
    outcome = _run_fake(terra_v4, "functional_failure", payload)
    snapshot = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    retained = snapshot["patch_retention"]["retained"]
    assert snapshot["failure_classification"] == "ORDINARY_WORKER_IMPLEMENTATION_FAILURE"
    assert snapshot["gate_records"][0]["status"] == "PASS"
    assert next(
        gate["status"] for gate in snapshot["gate_records"]
        if gate["gate_identity"] == "public_test_execution"
    ) == "FAIL"
    assert snapshot["worker_attempts"][0]["retained_artifact"] == retained
    artifact = outcome.root / "holdout" / "artifacts" / retained["retained_path"]
    artifact.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactRetentionError):
        verify_retained_artifact(
            run_artifact_root=outcome.root / "holdout" / "artifacts",
            retained_path=retained["retained_path"],
            artifact_id=retained["artifact_id"],
            expected_sha256=retained["sha256"],
            expected_size=retained["bytes"],
        )


def test_retention_failure_blocks_acceptance(monkeypatch, terra_v4):
    import redi_lab.holdout_transfer as transfer

    def fail_retention(**_kwargs):
        raise ArtifactRetentionError("simulated immutable evidence failure")

    monkeypatch.setattr(transfer, "retain_generated_bytes_artifact", fail_retention)
    outcome = _run_fake(terra_v4, "retention_failure", None)
    snapshot = replay_holdout(db_path=outcome.db_path, factory_run_id=outcome.factory_run_id)
    assert all(state == "PASS" for state in outcome.gate_outcomes.values())
    assert outcome.verified is False
    assert snapshot["failure_classification"] == "EVIDENCE_RETENTION_FAILURE"
    assert snapshot["patch_retention"]["retention_failures"]
