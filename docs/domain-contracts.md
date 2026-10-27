# REDI Lab Domain Contracts

Milestone: **REDI-01** — Domain Contracts & Event Model
Date: 2026-09-15
Status: Provider-independent Layer A.  Pure Python stdlib only (pytest for tests).
Package: `redi_lab.domain`

---

## Canonical contract inventory (14 + 1 event)

Source of truth: `REDI_LAB_RESEARCH_PLAN.md` §23 *Core Data Contracts*, verified against all six governance baseline documents.  No naming or count discrepancies were found during the REDI-01 contract-inventory audit.

| # | Contract | Purpose | Owns id type | Key fields |
|---|---|---|---|---|
| 1 | `ProductSpec` | Normalized product order; unknown requirements are explicit. | `ProductId` | outcome, requirements, constraints, acceptance, risk tier, unresolved, budget |
| 2 | `ProductClass` | Reusable production-similarity class. | `ProductClassId` | name, signature, compatible recipes, version, classification evidence |
| 3 | `ProductRecipe` | Versioned evidence-backed production configuration.  NOT a prompt template. | `RecipeId` | class id, DAG template ref, stations, worker pref/fallback, context budgets, gate/retry/escalation policies, rung (CANDIDATE/SHADOW/CANARY/VALIDATED), provenance evidence, validity conditions, revalidation state |
| 4 | `ProductDAG` | Versioned production graph (structural only; no scheduling). | graph id string | revision, nodes (DagNode), dependencies (DagDependency). Structural validation: duplicates, missing refs, self-loops, cycles |
| 5 | `StationContract` | Stable production role / responsibility.  Station ≠ Worker. No hard-coded provider. | `StationId` | role, accepted input/output kinds, owned/forbidden scope, allowed tool classes, applicable task classes, gate/handoff/eligibility rules |
| 6 | `WorkerProfile` | Replaceable candidate worker. | `WorkerId` | version, capability tags, context/tool/structured-output flags, station eligibility, cost reference, *opaque* provider reference |
| 7 | `TaskContract` | Bounded unit of work. No hidden evaluation data. | `TaskId` | goal, station id, allowed/forbidden scope, inputs/outputs/acceptance, prerequisites, budget/risk, escalation conditions |
| 8 | `ContextManifest` | Reproducibility contract describing exactly what a worker received. | `ContextManifestId` | task/station/product refs, sources, upstream handoffs, previous decisions, explicit exclusions, budget, deterministic hash |
| 9 | `HandoffManifest` | Structured station-to-station communication. | `HandoffManifestId` | producer/consumer scope, node/task refs, produced artifacts, guarantees, validation already performed, risks/open issues |
| 10 | `GateResult` | Distinguishes deterministic vs model-based verification. | `GateResultId` | gate identity & type, target artifact/task/node, criteria, PASS/FAIL/BLOCKED/ERROR, evidence, verifier type, failure reasons |
| 11 | `RunEvidence` | Normalized research observation for Evidence Ledger. | `EvidenceId` | experiment/factory run/product/recipe/station/worker refs, token usage, timing (wall vs compute), artifacts read/changed, gate outcomes, retry/escalation/intervention, cost, final status + failure category, environment |
| 12 | `FailureRecord` | Taxonomized failure record. | `FailureId` | exact 14-item canonical FailureCategory (see below), severity, recoverability, evidence ref |
| 13 | `ExperimentManifest` | Preregistered experiment per experiment-policy.md. | `ExperimentId` | title + hypothesis/question, arms, baselines, task selection, environment, worker declarations, context strategy, budgets, acceptance criteria & preregistered thresholds, randomization + repetition, measurement plan, pricing/version assumptions, intervention/replay policies, validity controls, preregistered-at |
| 14 | `FactoryRun` | One top-level production execution. | `FactoryRunId` | product spec id, DAG id, recipe id, mode (PRODUCTION/WARM/EXPLORATION/DRIFT), final status, verified boolean, timing/cost/tokens aggregate |
| 15 | `LifecycleEvent` | Domain event fact (normative 18-vocabulary set). | `EventId` | type, entity_ids, payload, timestamp.  See Event vocabulary below. |

### Supporting value objects

Value objects are defined in `redi_lab.domain.value_objects`:
`Requirement`, `AcceptanceCriterion`, `MonetaryCost`, `TokenUsage`, `Timing`, `Budget`,
`ResourceUsage`, `ArtifactRef`, `DagNode`, `DagDependency`, `EvidenceFieldRef`,
`ValidityCondition`, `EnvironmentInfo`.

Identity NewType ids live in `redi_lab.domain.identity`.

---

## Relationship map

```
ProductSpec ──(FactoryRun)──► ProductDAG ──► DagNode ⇄ DagDependency
                                        ▲
                                        │ references template_id
ProductClass ──── compatible recipes ──► ProductRecipe ──► rung + provenance + validity
     ▲
     └── candidate / definitive classification on ProductSpec

FactoryRun: connects ProductSpec, ProductDAG, (optional) Recipe,
            lifecycle Events, RunEvidence[], FailureRecord[] → final status.

TaskContract references StationContract.  DagNode references TaskContract+Station.

WorkerProfile  ─opaque provider data──►  outside domain layer / adapter only.
StationContract does NOT reference WorkerProfile directly; eligibility via station_eligibility on Worker.

ContextManifest references TaskContract, StationContract, upstream HandoffManifest IDs.
HandoffManifest references produced ArtifactRefs and consumer station scope.
GateResult references ArtifactRef, Task, node, evidence IDs.

RunEvidence is the "everything observed" leaf record; foreign keys into every
other top-level contract.  ExperimentManifest + FactoryRun + ProductSpec together
tell an ExperimentPolicy-compliant reproducibility story.
```

---

## Contract ownership / boundaries

| Layer | Owns | Never owns |
|---|---|---|
| **A. Domain (`redi_lab.domain`) — this package** | Typed contracts, enums, identity, structural validation, deterministic JSON serialization, schema versioning | Providers, database clients, HTTP clients, SDKs, factory behaviour, gate execution, scheduling, worker routing, persistence |
| **B. Adapters (later milestone)** | Provider SDKs, sandbox runtime, filesystem/git, HTTP calls, environment capture | Domain contracts that leak provider types into Layer A |
| **C. Deterministic orchestration (later)** | Scheduler, Context Compiler, Event Router runtime, deterministic gates, evidence ledger append-only writer, replay, recipe promotion ladder logic | Model judgment |
| **D. Model-based judgment (bounded, later)** | Configurator / Astra, ambiguous scope selection, drift scope narrowing, model-based reviewer gates | Any new top-level contract type |

Boundary enforcement mechanism for REDI-01: `tests/unit/test_domain_provider_independence.py` — static AST walk + runtime import audit.

---

## Serialization & versioning rules

1. **Schema version.** All top-level contracts carry an integer `schema_version`, defaulting to `SCHEMA_VERSION == 1` for the initial REDI-01 baseline.  Incompatible versions raise `SchemaVersionError` at decode time.  Migrations are out of scope.
2. **Deterministic JSON.** All domain contracts serialize via `redi_lab.domain.domain_to_json(obj)`, which:
   - uses `json.dumps(..., sort_keys=True, ensure_ascii=False)`;
   - encodes `Decimal` → string (precision preserved);
   - encodes `datetime` → ISO-8601 string;
   - encodes StrEnum values → string form;
   - encodes mapping keys deterministically;
   - stamps `__type__` so `domain_from_json` reconstructs the correct class.
3. **Round-trip equality.** For any contract `c`, `c == domain_from_json(domain_to_json(c))` holds when compared by JSON output (so tuple/list distinctions are not semantic).
4. **Unknown / optional values are preserved as `None`** — never invented as `0`, `""`, or `"USD"`.

---

## Event vocabulary (exact 18, architecture §5.1)

The canonical lifecycle event list follows architecture.md §5.1 tables top-to-bottom:

| Family | Events |
|---|---|
| Product lifecycle | `PRODUCT_ACCEPTED`, `PLAN_COMPILED`, `MILESTONE_COMPLETED`, `INTEGRATION_FAILED`, `PRODUCT_VERIFIED` |
| Node / Worker lifecycle | `NODE_READY`, `WORKER_STARTED`, `WORKER_COMPLETED`, `GATE_PASSED`, `GATE_FAILED`, `NODE_COMPLETED` |
| Retry / Escalation sub-lifecycle | `RETRY_REQUESTED`, `CONTEXT_REBUILD_REQUIRED`, `WORKER_ESCALATION_REQUIRED`, `ARCHITECTURAL_DECISION_REQUIRED` |
| Recipe / Optimization lifecycle | `RECIPE_CANDIDATE_CREATED`, `RECIPE_PROMOTED`, `DRIFT_DETECTED` |

Events are domain facts stored as immutable `LifecycleEvent` dataclasses.  No event execution handlers are implemented in REDI-01.

---

## Failure taxonomy (exact 14, plan §11 / charter terminology)

Used by `FailureCategory` enum and by every `GateResult.failure_reasons` / `RunEvidence.failure_category`.

```
01 specification_ambiguity
02 context_insufficiency
03 context_contamination
04 capability_insufficiency
05 tool_failure
06 environment_failure
07 dependency_mismatch
08 scope_violation
09 deterministic_test_failure
10 reviewer_rejection
11 integration_conflict
12 security_violation
13 timeout_or_budget_exhaustion
14 orchestrator_planning_error
```

No additional categories exist in the domain layer.  Convenience categories must be added only by revising this governance-blessed list.

---

## Deliberately deferred behavior (not in REDI-01)

Everything listed in the "NO IMPLEMENTATION OF THESE YET" section of the REDI-01 requirements:
OpenAI Agents SDK, Astra, LLM calls, worker execution & routing, Context Compiler, context selection, scheduler, event-router runtime, gate execution, sandbox, git-worktree manager, evidence persistence, SQLite, recipe learner, optimizer, drift detector, CLI, API, web UI, local model runtime, Docker, Temporal, background jobs, cloud deployment.

Also explicitly deferred:
- schema migrations beyond "raise on incompatible";
- similarity algorithms for ProductClass;
- concrete routing / promotion / downshift logic (only rung/ladder types exist).
