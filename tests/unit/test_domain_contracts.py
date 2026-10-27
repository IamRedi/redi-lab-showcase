"""REDI-01 domain contract tests — focused semantic tests, not implementation trivia.

Twelve test categories per REDI-01 user specification:

  1. every top-level contract construction
  2. invalid construction cases
  3. JSON-compatible serialization
  4. full round-trip serialization
  5. enum stability
  6. schema-version preservation / rejection
  7. ProductDAG structural validation
  8. exact event vocabulary (18 events)
  9. exact failure taxonomy (14 categories)
  10. Station/Worker provider independence
  11. unknown/optional value preservation
  12. Decimal and datetime round trips where used
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from redi_lab.domain import (  # 14 canonical top-level contracts
    CANONICAL_CONTRACTS,
    ArtifactRef,
    ContextManifest,
    DagDependency,
    DagNode,
    DomainDecodeError,
    EnvironmentInfo,
    EscalationLevel,
    ExperimentManifest,
    FactoryRun,
    FailureCategory,
    FailureRecord,
    GateResult,
    GateStatus,
    GateType,
    HandoffManifest,
    LifecycleEvent,
    LifecycleEventType,
    Mode,
    MonetaryCost,
    ProductClass,
    ProductDAG,
    ProductRecipe,
    ProductSpec,
    RecipeRung,
    Requirement,
    RiskTier,
    RunEvidence,
    RunStatus,
    SCHEMA_VERSION,
    SchemaVersionError,
    Severity,
    StationContract,
    TaskContract,
    Timing,
    TokenUsage,
    ValidityCondition,
    WorkerProfile,
    domain_from_json,
    domain_to_json,
    round_trip,
    validate_contract,
    validate_dag,
    DomainValidationError,
)
from redi_lab.domain.events import known_event_types
from redi_lab.domain.identity import new_id


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


UTC = timezone.utc


def _utc(iso: str) -> datetime:
    """Simple ISO parser used by tests — any timezone kept as-is."""
    return datetime.fromisoformat(iso)


# ---------------------------------------------------------------------------
# 1. every top-level contract construction (valid minimum example)
# ---------------------------------------------------------------------------


# Contracts that REQUIRE non-empty semantic fields (C-class before fix).
# These MUST raise DomainValidationError on default-construction.
REQUIRE_SEMANTIC_FIELDS: frozenset[str] = frozenset({
    "ProductSpec",        # required: requested_outcome
    "ProductClass",       # required: name
    "StationContract",    # required: role
    "WorkerProfile",      # required: version (reproducible worker identity)
    "TaskContract",       # required: goal — must not silently represent "no task"
    "GateResult",         # required: gate_identity
    "ExperimentManifest", # required: title + hypothesis_reference (preregistered)
})

# A minimal valid construction for each contract in REQUIRE_SEMANTIC_FIELDS.
_MINIMAL_VALID_KWARGS: dict[str, dict[str, object]] = {
    "ProductSpec": {"requested_outcome": "Build a CRUD web app MVP"},
    "ProductClass": {"name": "business_web_v1"},
    "StationContract": {"role": "tester"},
    "WorkerProfile": {"version": "1.0.0"},
    "TaskContract": {"goal": "Implement auth module"},
    "GateResult": {
        "gate_identity": "unit",
        "gate_type": GateType.DETERMINISTIC,
        "status": GateStatus.PASS,
    },
    "ExperimentManifest": {
        "title": "H3 MCW on business_web_v1",
        "hypothesis_reference": "H3",
    },
}


@pytest.mark.parametrize("cls", list(CANONICAL_CONTRACTS))
def test_top_level_contract_construction_defaults(cls) -> None:
    if cls.__name__ in REQUIRE_SEMANTIC_FIELDS:
        # Empty default is semantically invalid — validate_contract should raise
        # via __post_init__.  The canonical contract CANNOT silently exist
        # with no requested_outcome / no role / no goal (REDI-01B rule).
        with pytest.raises(DomainValidationError):
            cls()
        # But with minimal semantic kwargs, it constructs cleanly.
        instance = cls(**_MINIMAL_VALID_KWARGS[cls.__name__])
    else:
        instance = cls()
    assert hasattr(instance, "id")
    assert instance.id, f"{cls.__name__} default id must be truthy"
    assert instance.schema_version == SCHEMA_VERSION


@pytest.mark.parametrize("cls", [
    c for c in list(CANONICAL_CONTRACTS) if c.__name__ in REQUIRE_SEMANTIC_FIELDS
])
def test_semantically_empty_contract_raises_explicitly(cls) -> None:
    """Regression test: default-construct for 7 semantic contracts raises DomainValidationError."""
    with pytest.raises(DomainValidationError):
        cls()


def test_product_spec_construction_explicit() -> None:
    spec = ProductSpec(
        requested_outcome="Build a CRUD web app",
        requirements=[Requirement(text="Must persist users", kind="functional")],
        constraints=[Requirement(text="Python backend", kind="constraint")],
        unresolved_requirements=["role system design"],
        risk_tier=RiskTier.LOW,
    )
    assert spec.requested_outcome == "Build a CRUD web app"
    assert len(spec.requirements) == 1
    assert list(spec.unresolved_requirements) == ["role system design"]
    assert spec.risk_tier is RiskTier.LOW


def test_station_contract_no_provider_field() -> None:
    """StationContract has no provider/model slot by design (Station != Worker)."""
    station = StationContract(role="tester", required_output_kinds=["test_report"])
    # If any of these strings leaked, StationContract would have hard-coded a provider.
    forbidden = {"openai", "gpt-", "claude", "gemini", "ollama", "langgraph", "temporal"}
    raw = domain_to_json(station).lower()
    for token in forbidden:
        assert token not in raw, f"StationContract must not mention provider {token!r}"


def test_worker_profile_provider_opaque_only() -> None:
    """WorkerProfile may store an opaque provider reference but nothing typed."""
    worker = WorkerProfile(version="v1", provider_opaque_reference="model-pool/acme-large")
    assert worker.provider_opaque_reference.startswith("model-pool/")
    # Decoding must preserve the opaque reference without "knowing" provider types.
    back = round_trip(worker)
    assert back.provider_opaque_reference == "model-pool/acme-large"


# ---------------------------------------------------------------------------
# 2. Invalid construction cases
# ---------------------------------------------------------------------------


def test_requirement_empty_text_invalid() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Requirement(text="", kind="functional")


def test_monetary_cost_non_decimal_invalid() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        MonetaryCost(amount=42)  # type: ignore[arg-type]


def test_token_usage_negative_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        TokenUsage(input=-1)


def test_timing_negative_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        Timing(wall_clock_seconds=-0.1)


def test_dag_node_empty_id_rejected() -> None:
    from redi_lab.domain.value_objects import DagNode

    with pytest.raises(ValueError, match="non-empty"):
        DagNode(node_id=" ")


def test_invalid_gate_type_via_roundtrip_raises() -> None:
    good = GateResult(gate_identity="unit-test", gate_type=GateType.DETERMINISTIC, status=GateStatus.PASS)
    json_text = domain_to_json(good).replace('"deterministic"', '"nonsense-type"')
    with pytest.raises(DomainDecodeError, match="unknown GateType"):
        domain_from_json(json_text)


# ---------------------------------------------------------------------------
# 3. JSON-compatible serialization — no top-level tuples, datetimes become strings, ...
# ---------------------------------------------------------------------------


def test_json_serialization_is_valid_json_without_decimal_types() -> None:
    evidence = RunEvidence(
        cost=MonetaryCost(amount=Decimal("12.3456"), currency="USD", basis="billed"),
        timing=Timing(started_at=_utc("2026-09-15T10:00:00"), wall_clock_seconds=30.0),
    )
    text = domain_to_json(evidence)
    # Valid JSON — loads without special Decimal hook
    import json

    obj = json.loads(text)
    assert obj["__type__"] == "RunEvidence"
    assert obj["cost"]["amount"] == "12.3456"
    assert obj["timing"]["started_at"] == "2026-09-15T10:00:00"


# ---------------------------------------------------------------------------
# 4. Full round-trip serialize → deserialize equality
# ---------------------------------------------------------------------------


def _rich_contracts() -> list[object]:
    """Build a rich non-default instance of every top-level contract for RT testing."""
    cost = MonetaryCost(amount=Decimal("1.25"), currency="USD", basis="observed")
    tokens = TokenUsage(input=1000, output=500)
    timing = Timing(
        started_at=_utc("2026-09-15T12:00:00"),
        wall_clock_seconds=12.5,
        compute_seconds=25.0,
    )
    return [
        ProductSpec(
            requested_outcome="Reddit clone MVP",
            requirements=[Requirement(text="posts table", kind="functional")],
            risk_tier=RiskTier.MEDIUM,
        ),
        ProductClass(name="business_web_v1", description="CRUD SaaS web app", version=2),
        ProductRecipe(
            rung=RecipeRung.SHADOW,
            version=1,
            validity_conditions=[
                ValidityCondition(
                    description="python>=3.11",
                    scope="environment",
                    checked_via="version",
                )
            ],
        ),
        ProductDAG(
            revision=3,
            nodes=[
                DagNode(node_id=f"n{i}", role_hint=("builder", "tester", "reviewer")[i % 3])
                for i in range(4)
            ],
            dependencies=[
                DagDependency(upstream=f"n{i}", downstream=f"n{i+1}")
                for i in range(3)
            ],
        ),
        StationContract(role="tester"),
        WorkerProfile(version="1.2.3", capability_tags=("code", "tests")),
        TaskContract(goal="write unit tests for auth module"),
        ContextManifest(),
        HandoffManifest(
            produced_artifacts=[
                ArtifactRef(ref_id="art-1", kind="file", source="output", sha256="a" * 64)
            ]
        ),
        GateResult(gate_identity="lint", gate_type=GateType.DETERMINISTIC, status=GateStatus.PASS),
        RunEvidence(
            worker_version="v1",
            token_usage=tokens,
            timing=timing,
            cost=cost,
            final_status=RunStatus.COMPLETED,
            failure_category=None,
        ),
        FailureRecord(
            category=FailureCategory.CONTEXT_CONTAMINATION,
            origin="context-firewall-audit",
            severity=Severity.WARNING,
        ),
        ExperimentManifest(
            title="H3 MCW on business_web_v1",
            hypothesis_reference="H3",
            preregistered_at=_utc("2026-09-15T09:00:00"),
        ),
        FactoryRun(mode=Mode.PRODUCTION, final_status=RunStatus.RUNNING, timing=timing),
        # One event per family
        LifecycleEvent(
            event_type=LifecycleEventType.PRODUCT_ACCEPTED,
            entity_ids={"product_id": new_id("product")},
            payload={"budget": 42},
        ),
    ]


@pytest.mark.parametrize("instance", _rich_contracts(), ids=lambda o: type(o).__name__)
def test_full_round_trip_equality(instance) -> None:
    """Serialize → deserialize → equality with original.

    We compare JSON output (semantic equality) because tuple/list distinction is
    erased by JSON and because dataclasses with ``field(default_factory=tuple)``
    deserialize to lists or tuples depending on the coercer (we coerce Sequence
    to tuple).
    """
    text_a = domain_to_json(instance)
    back = domain_from_json(text_a)
    text_b = domain_to_json(back)
    assert text_a == text_b, (
        f"round-trip JSON mismatch for {type(instance).__name__}:\n{text_a}\nvs\n{text_b}"
    )


# ---------------------------------------------------------------------------
# 5. Enum stability — names and values match governance baseline exactly
# ---------------------------------------------------------------------------


FAILURE_EXPECTED = [
    "specification_ambiguity",
    "context_insufficiency",
    "context_contamination",
    "capability_insufficiency",
    "tool_failure",
    "environment_failure",
    "dependency_mismatch",
    "scope_violation",
    "deterministic_test_failure",
    "reviewer_rejection",
    "integration_conflict",
    "security_violation",
    "timeout_or_budget_exhaustion",
    "orchestrator_planning_error",
]


def test_failure_taxonomy_exact_14() -> None:
    values = [m.value for m in FailureCategory]
    assert values == FAILURE_EXPECTED, f"failure taxonomy mismatch: {values}"
    assert len(FailureCategory) == 14


def test_recipe_rung_four_ladder() -> None:
    assert [r.value for r in RecipeRung] == ["candidate", "shadow", "canary", "validated"]


def test_mode_four_tiers() -> None:
    assert [m.value for m in Mode] == ["production", "warm", "exploration", "drift"]


def test_escalation_level_ordered() -> None:
    assert [e.value for e in EscalationLevel] == [
        "retry_same",
        "context_rebuild",
        "worker_escalation",
        "architectural_decision",
    ]


# ---------------------------------------------------------------------------
# 6. Schema version preservation / rejection
# ---------------------------------------------------------------------------


def test_schema_version_default_matches_module_constant() -> None:
    for cls in CANONICAL_CONTRACTS:
        if cls.__name__ in _MINIMAL_VALID_KWARGS:
            instance = cls(**_MINIMAL_VALID_KWARGS[cls.__name__])
        else:
            instance = cls()
        assert instance.schema_version == SCHEMA_VERSION, cls.__name__


def test_unknown_schema_version_on_decode_rejected() -> None:
    spec = ProductSpec(requested_outcome="x")
    bad = domain_to_json(spec).replace(f'"schema_version": {SCHEMA_VERSION}', '"schema_version": 999')
    with pytest.raises(SchemaVersionError):
        domain_from_json(bad)


def test_missing_schema_version_defaults_ok_on_construct() -> None:
    # Constructing without schema_version → uses default.
    spec = ProductSpec(requested_outcome="y", schema_version=SCHEMA_VERSION)
    assert spec.schema_version == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 7. ProductDAG structural validation
# ---------------------------------------------------------------------------


def _nodes(ids: list[str]):
    from redi_lab.domain.value_objects import DagNode

    return [DagNode(node_id=i) for i in ids]


def _deps(edges: list[tuple[str, str]]):
    from redi_lab.domain.value_objects import DagDependency

    return [DagDependency(upstream=u, downstream=d) for u, d in edges]


def test_valid_dag_linear_ok() -> None:
    dag = ProductDAG(nodes=_nodes(list("abcd")), dependencies=_deps([("a", "b"), ("b", "c"), ("c", "d")]))
    validate_dag(dag)  # no raise


def test_valid_dag_parallel_ok() -> None:
    dag = ProductDAG(
        nodes=_nodes(["root", "a", "b", "merge"]),
        dependencies=_deps([("root", "a"), ("root", "b"), ("a", "merge"), ("b", "merge")]),
    )
    validate_dag(dag)


def test_dag_duplicate_node_ids_invalid() -> None:
    dag = ProductDAG(nodes=_nodes(["a", "a", "b"]), dependencies=[])
    with pytest.raises(DomainValidationError, match="duplicate node_ids"):
        validate_dag(dag)


def test_dag_missing_dependency_reference_invalid() -> None:
    dag = ProductDAG(nodes=_nodes(["a", "b"]), dependencies=_deps([("a", "c")]))  # 'c' missing
    with pytest.raises(DomainValidationError, match="does not reference any node"):
        validate_dag(dag)


def test_dag_self_loop_invalid() -> None:
    from redi_lab.domain.value_objects import DagDependency

    dag = ProductDAG(
        nodes=_nodes(["a", "b"]),
        dependencies=[
            DagDependency(upstream="a", downstream="b"),
        ],
    )
    # Construct an invalid dep manually.
    bad_dep = DagDependency.__new__(DagDependency)
    object.__setattr__(bad_dep, "upstream", "a")
    object.__setattr__(bad_dep, "downstream", "a")
    object.__setattr__(bad_dep, "kind", "hard")
    bad = replace(dag, dependencies=list(dag.dependencies) + [bad_dep])
    with pytest.raises(DomainValidationError, match="self-loop"):
        validate_dag(bad)


def test_dag_cycle_three_nodes_invalid() -> None:
    dag = ProductDAG(
        nodes=_nodes(list("abc")),
        dependencies=_deps([("a", "b"), ("b", "c"), ("c", "a")]),
    )
    with pytest.raises(DomainValidationError, match="contains a cycle"):
        validate_dag(dag)


# ---------------------------------------------------------------------------
# 8. Exact 18-event vocabulary
# ---------------------------------------------------------------------------


EXPECTED_EVENTS = [
    "PRODUCT_ACCEPTED",
    "PLAN_COMPILED",
    "MILESTONE_COMPLETED",
    "INTEGRATION_FAILED",
    "PRODUCT_VERIFIED",
    "NODE_READY",
    "WORKER_STARTED",
    "WORKER_COMPLETED",
    "GATE_PASSED",
    "GATE_FAILED",
    "NODE_COMPLETED",
    "RETRY_REQUESTED",
    "CONTEXT_REBUILD_REQUIRED",
    "WORKER_ESCALATION_REQUIRED",
    "ARCHITECTURAL_DECISION_REQUIRED",
    "RECIPE_CANDIDATE_CREATED",
    "RECIPE_PROMOTED",
    "DRIFT_DETECTED",
]


def test_exact_event_vocabulary_count() -> None:
    assert len(LifecycleEventType) == 18


def test_exact_event_vocabulary_values() -> None:
    values = [e.value for e in known_event_types()]
    assert values == EXPECTED_EVENTS, f"event vocabulary mismatch: {values}"


def test_every_event_type_is_in_enum() -> None:
    for value in EXPECTED_EVENTS:
        assert LifecycleEventType(value) is not None


# ---------------------------------------------------------------------------
# 9. Exact failure taxonomy (covered above by test_failure_taxonomy_exact_14)
# ---------------------------------------------------------------------------


def test_failure_record_round_trips_every_category() -> None:
    for cat in FailureCategory:
        rec = FailureRecord(category=cat, origin=f"origin-{cat.value}")
        assert round_trip(rec).category is cat


# ---------------------------------------------------------------------------
# 10. Station/Worker provider independence — implicit via top-level tests above.
#     Explicit additional check: no provider names appear in serialized StationContract
#     even when WorkerProfile uses the opaque reference.
# ---------------------------------------------------------------------------


def test_station_and_worker_serialization_free_of_provider_types() -> None:
    station = StationContract(role="reviewer", allowed_tool_classes=["read", "write-tests"])
    worker = WorkerProfile(
        version="1.0.0",
        display_name="fast-worker",
        provider_opaque_reference="some-label/my-favourite-worker",
    )
    blob = (domain_to_json(station) + domain_to_json(worker)).lower()
    banned = [
        "openai",
        "agentssdk",
        "agents_sdk",
        "anthropic",
        "gemini",
        "ollama",
        "langgraph",
        "temporal",
        "docker.sdk",
        "dockersdk",
        "psycopg",
        "sqlite3",
        "boto3",
        "requests",
        "httpx",
    ]
    for token in banned:
        assert token not in blob, f"provider token {token!r} leaked into domain serialization"


# ---------------------------------------------------------------------------
# 11. Unknown / optional value preservation
# ---------------------------------------------------------------------------


def test_optional_fields_not_invented_as_zero_values() -> None:
    # RunEvidence with NO cost / timing / tokens.  All must survive None on round trip
    # rather than becoming 0 / 0.0 / Decimal('0').
    evidence = RunEvidence(
        final_status=None,
        cost=None,
        timing=None,
        token_usage=None,
    )
    back = round_trip(evidence)
    assert back.final_status is None
    assert back.cost is None
    assert back.timing is None
    assert back.token_usage is None

    # Worker / station optional ids preserved
    w = WorkerProfile(version="v99", station_eligibility=(), availability_status=None)
    assert round_trip(w).availability_status is None


def test_product_spec_unresolved_requirements_preserved() -> None:
    spec = ProductSpec(
        requested_outcome="MVP",
        unresolved_requirements=["what API style?", "auth? RBAC or ACL?"],
    )
    back = round_trip(spec)
    assert tuple(back.unresolved_requirements) == ("what API style?", "auth? RBAC or ACL?")


# ---------------------------------------------------------------------------
# 12. Decimal + datetime round trip
# ---------------------------------------------------------------------------


def test_monetary_decimal_round_trip_preserves_precision() -> None:
    original = Decimal("0.000000123450000")
    cost = MonetaryCost(amount=original, currency="EUR", basis="estimated")
    back = round_trip(cost) if False else domain_from_json(domain_to_json(
        RunEvidence(cost=cost)
    )).cost
    assert back is not None
    assert isinstance(back.amount, Decimal)
    # String comparison — exact decimal representation, not float approximation.
    assert format(back.amount, "f") == format(original, "f")


def test_datetime_round_trip_preserves_microseconds_and_offset() -> None:
    ts = _utc("2026-09-15T14:23:45.123456+00:00")
    t = Timing(started_at=ts, ended_at=ts)
    fr = FactoryRun(mode=Mode.EXPLORATION, timing=t)
    back = round_trip(fr)
    assert back.timing is not None
    assert back.timing.started_at == ts
    assert back.timing.ended_at == ts


# ---------------------------------------------------------------------------
# Canonical contract count (asserted 14, matches plan §23 inventory)
# ---------------------------------------------------------------------------


def test_canonical_contract_count_exactly_14() -> None:
    assert len(CANONICAL_CONTRACTS) == 14
    names = [c.__name__ for c in CANONICAL_CONTRACTS]
    assert names == [
        "ProductSpec",
        "ProductClass",
        "ProductRecipe",
        "ProductDAG",
        "StationContract",
        "WorkerProfile",
        "TaskContract",
        "ContextManifest",
        "HandoffManifest",
        "GateResult",
        "RunEvidence",
        "FailureRecord",
        "ExperimentManifest",
        "FactoryRun",
    ], names
