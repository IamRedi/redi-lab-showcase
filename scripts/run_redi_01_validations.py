"""REDI-01 manual validation runner — runs every assertion in the test suite without pytest.

Produces TAP-like output, non-zero exit on first failure.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from decimal import Decimal
from pathlib import Path


FAIL = 0
TOTAL = 0

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

REPO_ROOT = ROOT.parent
# Ensure we import the live repository redi_lab/ (HEAD of this repo checkout), not
# any stale copy that may exist in a virtual-environment's site-packages. The REDI
# validation runners deliberately pin source-of-truth to the repository checkout
# so that contract regressions are caught regardless of venv install state.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Contracts that require non-empty semantic fields now enforce them at
# construction time via __post_init__ → DomainValidationError.  Construct
# with these minimal kwargs when validating identity / schema_version.
_MIN_KWARGS: dict[str, dict[str, object]] = {
    "ProductSpec": {"requested_outcome": "validate default id/sv"},
    "ProductClass": {"name": "ValidName"},
    "StationContract": {"role": "role_placeholder"},
    "WorkerProfile": {"version": "v1"},
    "TaskContract": {"goal": "dummy goal for id check"},
    "GateResult": {"gate_identity": "gate-001"},
    "ExperimentManifest": {"title": "Pilot calibration", "hypothesis_reference": "H1"},
}


def _construct_valid(cls):
    """Construct cls, using minimal kwargs if needed to pass __post_init__."""
    return cls(**_MIN_KWARGS.get(cls.__name__, {}))


def check(description: str, condition, detail: str = "") -> None:
    global TOTAL, FAIL
    TOTAL += 1
    if condition:
        print(f"ok {TOTAL:03d} - {description}")
    else:
        FAIL += 1
        print(f"not ok {TOTAL:03d} - {description}")
        if detail:
            for line in detail.splitlines():
                print(f"    # {line}")


def raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, exc_type):
            return True, f"raised {type(e).__name__}: {e}"
        return False, f"wrong exception: {type(e).__name__}: {e}"
    return False, f"did not raise (expected {exc_type.__name__})"


def run() -> int:
    import redi_lab.domain as dom

    # --- Import / basic checks -------------------------------------------------
    check("SCHEMA_VERSION equals 1 (initial baseline)", dom.SCHEMA_VERSION == 1)
    contracts_module = importlib.import_module("redi_lab.domain.contracts")
    check(
        "CANONICAL_CONTRACTS has exactly 14 top-level contracts",
        len(contracts_module.CANONICAL_CONTRACTS) == 14,
        detail=f"got {len(contracts_module.CANONICAL_CONTRACTS)}: {[c.__name__ for c in contracts_module.CANONICAL_CONTRACTS]}",
    )

    # --- 1. top-level construction -------------------------------------------
    # REDI-01B hardening: 7 contracts now RAISE DomainValidationError for
    # empty required semantic fields (no silently-invalid empty state).
    for cls in contracts_module.CANONICAL_CONTRACTS:
        name = cls.__name__
        if name in _MIN_KWARGS:
            # Must raise explicitly for empty defaults — cannot silently exist.
            raised, detail_msg = raises(dom.DomainValidationError, cls)
            check(f"{name}: empty-construct raises DomainValidationError (not silent invalid)", raised, detail=detail_msg)
            # Construct with minimal valid kwargs → id + schema_version present
            instance = _construct_valid(cls)
            ok = (
                getattr(instance, "id", None)
                and getattr(instance, "schema_version", None) == dom.SCHEMA_VERSION
            )
            check(f"{name}: minimal-valid construct has id + schema_version", ok)
        else:
            # No semantic required fields: default-construct valid.
            instance = cls()
            ok = (
                getattr(instance, "id", None)
                and getattr(instance, "schema_version", None) == dom.SCHEMA_VERSION
            )
            check(f"{name} default construction is valid", ok)

    ps = dom.ProductSpec(
        requested_outcome="Build a CRUD web app",
        requirements=[dom.Requirement(text="Must persist users", kind="functional")],
        constraints=[dom.Requirement(text="Python backend", kind="constraint")],
        unresolved_requirements=["role system design"],
        risk_tier=dom.RiskTier.LOW,
    )
    check(
        "ProductSpec rich construction round-trips outcome",
        ps.requested_outcome == "Build a CRUD web app"
        and ps.risk_tier is dom.RiskTier.LOW
        and len(ps.requirements) == 1,
    )

    # --- Station != Worker / provider-independence static check ---------------
    station = dom.StationContract(role="tester", required_output_kinds=["test_report"])
    blob = dom.domain_to_json(station).lower()
    leaked = [t for t in ("openai", "gemini", "anthropic", "ollama", "langgraph", "temporal") if t in blob]
    check("StationContract serialized form never names a provider", not leaked, detail=str(leaked))

    # --- 2. invalid construction cases ----------------------------------------
    ok, det = raises(ValueError, lambda: dom.Requirement(text="", kind="functional"))
    check("Requirement with empty text raises ValueError", ok, detail=det)

    ok, det = raises(TypeError, lambda: dom.MonetaryCost(amount=42))
    check("MonetaryCost requires Decimal amount, rejects int", ok, detail=det)

    ok, det = raises(ValueError, lambda: dom.TokenUsage(input=-1))
    check("TokenUsage rejects negative input count", ok, detail=det)

    ok, det = raises(ValueError, lambda: dom.Timing(wall_clock_seconds=-0.1))
    check("Timing rejects negative wall clock", ok, detail=det)

    DagNode = importlib.import_module("redi_lab.domain.value_objects").DagNode
    ok, det = raises(ValueError, lambda: DagNode(node_id=" "))
    check("DagNode with empty id is invalid", ok, detail=det)

    good = dom.GateResult(
        gate_identity="unit", gate_type=dom.GateType.DETERMINISTIC, status=dom.GateStatus.PASS
    )
    bad_json = dom.domain_to_json(good).replace('"deterministic"', '"not-a-type"')
    ok, det = raises(dom.DomainDecodeError, lambda: dom.domain_from_json(bad_json))
    check("Unknown GateType enum fails decode with DomainDecodeError", ok, detail=det)

    # --- 3. JSON-compatible serialization -------------------------------------
    evidence = dom.RunEvidence(
        cost=dom.MonetaryCost(amount=Decimal("12.3456"), currency="USD", basis="billed"),
    )
    import json
    raw = json.loads(dom.domain_to_json(evidence))
    check(
        "RunEvidence JSON encodes Decimal as string, not float",
        raw["cost"]["amount"] == "12.3456" and isinstance(raw["cost"]["amount"], str),
    )
    check(
        "Top-level __type__ annotation present on JSON",
        raw["__type__"] == "RunEvidence",
    )

    # --- 4. full round-trip ----------------------------------------------------
    val = importlib.import_module("redi_lab.domain.value_objects")
    ArtifactRef = val.ArtifactRef
    DagDependency = val.DagDependency
    instances = [
        dom.ProductSpec(requested_outcome="x", risk_tier=dom.RiskTier.MEDIUM),
        dom.ProductClass(name="business_web_v1", description="CRUD SaaS", version=2),
        dom.ProductRecipe(rung=dom.RecipeRung.SHADOW, version=1),
        dom.ProductDAG(
            revision=3,
            nodes=[DagNode(node_id=f"n{i}") for i in range(4)],
            dependencies=[DagDependency(upstream=f"n{i}", downstream=f"n{i+1}") for i in range(3)],
        ),
        dom.StationContract(role="tester"),
        dom.WorkerProfile(version="1.2.3", capability_tags=("code", "tests")),
        dom.TaskContract(goal="write unit tests for auth module"),
        dom.ContextManifest(),
        dom.HandoffManifest(produced_artifacts=[ArtifactRef(ref_id="art-1", kind="file")]),
        dom.GateResult(
            gate_identity="lint",
            gate_type=dom.GateType.DETERMINISTIC,
            status=dom.GateStatus.PASS,
        ),
        dom.RunEvidence(
            token_usage=dom.TokenUsage(input=1000, output=500),
            timing=dom.Timing(wall_clock_seconds=12.5, compute_seconds=25.0),
            cost=dom.MonetaryCost(amount=Decimal("1.25"), currency="USD"),
            final_status=dom.RunStatus.COMPLETED,
        ),
        dom.FailureRecord(category=dom.FailureCategory.CONTEXT_CONTAMINATION, origin="cfa"),
        dom.ExperimentManifest(title="H3 MCW on business_web_v1", hypothesis_reference="H3"),
        dom.FactoryRun(mode=dom.Mode.PRODUCTION, final_status=dom.RunStatus.RUNNING),
        dom.LifecycleEvent(
            event_type=dom.LifecycleEventType.PRODUCT_ACCEPTED,
            entity_ids={"product_id": dom.new_id("product")},
            payload={"budget": 42},
        ),
    ]
    for inst in instances:
        text_a = dom.domain_to_json(inst)
        try:
            text_b = dom.domain_to_json(dom.domain_from_json(text_a))
        except Exception as exc:  # noqa: BLE001
            check(f"Round-trip {type(inst).__name__}", False, detail=f"decode failed: {exc}\n{traceback.format_exc(limit=2)}")
            continue
        ok = text_a == text_b
        detail = "" if ok else f"JSON differs:\nA:{text_a[:220]}\nB:{text_b[:220]}"
        check(f"Round-trip {type(inst).__name__}", ok, detail=detail)

    # --- 5. enum stability -----------------------------------------------------
    FAILURE_ORDER = [
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
    values = [m.value for m in dom.FailureCategory]
    check(
        "Failure taxonomy: exactly 14 categories in canonical order",
        values == FAILURE_ORDER and len(dom.FailureCategory) == 14,
        detail=str(values),
    )
    check(
        "Recipe rung order (4-rung ladder)",
        [r.value for r in dom.RecipeRung] == ["candidate", "shadow", "canary", "validated"],
    )
    check(
        "Mode 4-tier",
        [m.value for m in dom.Mode] == ["production", "warm", "exploration", "drift"],
    )

    # --- 6. schema version preservation / rejection ---------------------------
    for cls in contracts_module.CANONICAL_CONTRACTS:
        check(f"{cls.__name__} defaults schema_version to module constant",
              _construct_valid(cls).schema_version == 1)

    bad = dom.domain_to_json(dom.ProductSpec(requested_outcome="x")).replace(
        f'"schema_version": {dom.SCHEMA_VERSION}',
        '"schema_version": 999',
    )
    ok, det = raises(dom.SchemaVersionError, lambda: dom.domain_from_json(bad))
    check("Schema version 999 raises SchemaVersionError on decode", ok, detail=det)

    # --- 7. ProductDAG structural validation ----------------------------------
    nodes_linear = [DagNode(node_id=i) for i in list("abcd")]
    deps_linear = [DagDependency(upstream=list("abcd")[i], downstream=list("abcd")[i + 1]) for i in range(3)]
    ok, det = True, ""
    try:
        dom.validate_dag(dom.ProductDAG(nodes=nodes_linear, dependencies=deps_linear))
    except Exception as exc:  # noqa: BLE001
        ok, det = False, f"{type(exc).__name__}: {exc}"
    check("Valid linear DAG passes structural validation", ok, detail=det)

    dag_dup = dom.ProductDAG(nodes=[DagNode(node_id="a"), DagNode(node_id="a"), DagNode(node_id="b")])
    ok, det = raises(dom.DomainValidationError, lambda: dom.validate_dag(dag_dup))
    check("Duplicate DAG node_ids fail validation", ok, detail=det)

    dag_missing = dom.ProductDAG(
        nodes=[DagNode(node_id="a"), DagNode(node_id="b")],
        dependencies=[DagDependency(upstream="a", downstream="c")],
    )
    ok, det = raises(dom.DomainValidationError, lambda: dom.validate_dag(dag_missing))
    check("DAG dependency referencing missing node fails validation", ok, detail=det)

    # self-loop via DagDependency direct attrs
    dd = DagDependency.__new__(DagDependency)
    object.__setattr__(dd, "upstream", "a")
    object.__setattr__(dd, "downstream", "a")
    object.__setattr__(dd, "kind", "hard")
    bad_self = dom.ProductDAG(
        nodes=[DagNode(node_id="a"), DagNode(node_id="b")],
        dependencies=[DagDependency(upstream="a", downstream="b"), dd],
    )
    ok, det = raises(dom.DomainValidationError, lambda: dom.validate_dag(bad_self))
    check("DAG self-loop fails validation", ok, detail=det)

    dag_cycle = dom.ProductDAG(
        nodes=[DagNode(node_id=i) for i in list("abc")],
        dependencies=[
            DagDependency(upstream="a", downstream="b"),
            DagDependency(upstream="b", downstream="c"),
            DagDependency(upstream="c", downstream="a"),
        ],
    )
    ok, det = raises(dom.DomainValidationError, lambda: dom.validate_dag(dag_cycle))
    check("DAG 3-node cycle fails validation", ok, detail=det)

    # --- 8. exact 18-event vocabulary -----------------------------------------
    from redi_lab.domain.events import known_event_types

    EXPECTED_18 = [
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
    values = [e.value for e in known_event_types()]
    check(
        f"Lifecycle event vocabulary: exactly {len(EXPECTED_18)} events, matches architecture §5.1",
        values == EXPECTED_18 and len(dom.LifecycleEventType) == 18,
        detail=f"got values={values}",
    )

    # --- 10. provider independence AST audit ----------------------------------
    DOM_ROOT = Path(dom.__file__).resolve().parent
    FORBIDDEN = (
        "openai",
        "agents",
        "google",
        "gemini",
        "anthropic",
        "ollama",
        "langgraph",
        "langchain",
        "temporalio",
        "temporal",
        "docker",
        "psycopg",
        "pg8000",
        "sqlite3",
        "sqlalchemy",
        "httpx",
        "aiohttp",
        "requests",
        "boto3",
        "botocore",
        "azure",
        "gcloud",
        "pydantic",
    )
    import ast
    violations = 0
    for py in DOM_ROOT.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    break
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mod = node.module
            if mod:
                for f in FORBIDDEN:
                    if mod == f or mod.startswith(f + "."):
                        violations += 1
                        print(f"  # violation in {py}: import {mod}")
    check("AST provider-independence audit of domain source passes", violations == 0, detail=f"{violations} forbidden imports found")

    # --- 11. unknown / optional value preservation -----------------------------
    ev = dom.RunEvidence(cost=None, timing=None, token_usage=None, final_status=None)
    back = dom.domain_from_json(dom.domain_to_json(ev))
    check(
        "Optional/absent RunEvidence fields come back as None, not zeroes",
        back.cost is None and back.timing is None and back.token_usage is None and back.final_status is None,
    )
    spec = dom.ProductSpec(
        requested_outcome="MVP", unresolved_requirements=["what API?", "auth style?"]
    )
    back = dom.domain_from_json(dom.domain_to_json(spec))
    check(
        "ProductSpec unresolved_requirements preserved over round-trip",
        tuple(back.unresolved_requirements) == ("what API?", "auth style?"),
    )

    # --- 12. Decimal + datetime round trip ------------------------------------
    from datetime import datetime

    ts = datetime.fromisoformat("2026-09-15T14:23:45.123456")
    original = Decimal("0.000000123450000")
    ev = dom.RunEvidence(
        cost=dom.MonetaryCost(amount=original, currency="EUR"),
        timing=dom.Timing(started_at=ts, ended_at=ts),
    )
    back = dom.domain_from_json(dom.domain_to_json(ev))
    check(
        "Decimal cost survives as exact Decimal representation",
        isinstance(back.cost.amount, Decimal) and f"{back.cost.amount:.15f}" == f"{original:.15f}",
        detail=f"got back={back.cost.amount} type={type(back.cost.amount).__name__}",
    )
    check(
        "Datetime timing survives exact over JSON round-trip",
        back.timing.started_at == ts and back.timing.ended_at == ts,
        detail=f"started_at round-trip: {back.timing.started_at!r} != {ts!r}",
    )

    # --- Summary ---------------------------------------------------------------
    print()
    print(f"1..{TOTAL}")
    print(f"# Passed {TOTAL - FAIL} / {TOTAL}")
    if FAIL:
        print(f"# FAILED {FAIL} tests — see `not ok` rows above")
        return 1
    print("# All REDI-01 contract validations passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
