# REDI Lab Logical Architecture

Document version: 1.0
Date: 2026-09-15
Milestone: REDI-00A
Status: Frozen pending REDI-00A validation

---

## 1. Purpose and Scope

This document defines the **logical architecture** of the REDI Lab research platform. It describes:
- The components of the system, their responsibilities, and their interfaces.
- The lifecycle event model: which events exist, who emits them, and who handles them.
- The explicit architectural boundaries that separate REDI Method concepts from provider adapters, deterministic orchestration from model-based judgment, and management state from worker state.
- The architectural principles that govern all design and implementation decisions in subsequent milestones.

This document is explicitly **not** an implementation specification. It does not prescribe databases, programming languages, queue systems, UI frameworks, or deployment topologies. It does not require OpenAI Agents SDK, Temporal, Docker, SQLite, or any specific runtime. Those may be chosen as implementation details behind adapters in later milestones, provided they respect the boundaries and contracts defined here.

The core requirement: **the REDI Method domain layer must remain provider-independent, model-provider-independent, and runtime-independent throughout the research program.** No domain object, contract, ledger entry, or recipe may depend directly on OpenAI-specific types, Gemini-specific types, LangGraph types, or any other vendor- or runtime-specific type.

---

## 2. Architectural Principles

These principles are normative. Any proposed design change, implementation choice, or optimization in subsequent milestones that violates these principles is rejected unless the principle itself is revised through the charter/architecture change-control process (Section 7).

### P1 — Contracts over Conversations
Inter-component communication is structured through versioned, schema-validated artifacts (contracts, manifests, handoffs, events) not shared chat history, natural-language notes, or free-form messages. Every state transition is a typed event with a schema version.

### P2 — Independent Verification
A worker that produces an output cannot be the sole authority that the output is correct. Quality gates are layered: deterministic gates first, then independent model-based judgment, never self-approval by the producing trajectory.

### P3 — Deterministic Logic before LLM Judgment
Every decision point in the factory is first expressed as a deterministic software rule (budgets, thresholds, schemas, dependency checks, retry policies). LLM-based judgment is invoked only when the deterministic policy explicitly delegates to it and only within a bounded scope. LLM outputs are always schema-validated and never trusted as-is.

### P4 — Orchestrator Holds Management State, Not Full Implementation History
The Factory Configurator / Orchestrator component knows what is being produced, the dependency state, risk flags, escalation status, and budget. It does NOT accumulate every file change, every line of code, every tool return value, or every worker reasoning step. Those live in workspaces, traces, and the Evidence Ledger. The orchestrator consults them only when a management decision requires it.

### P5 — Stations Are Stable, Workers Are Replaceable
A StationContract defines a stable responsibility in the production process. Stations survive across model generations, provider changes, and runtime updates. A Worker is a replaceable execution unit behind the station; a station can have a pool of interchangeable workers, and changing the worker pool does not require changing the StationContract or any downstream consumer.

### P6 — Unknown Products Explore; Known Products Exploit Validated Recipes
The factory explicitly distinguishes between cold-start exploration mode (no validated recipe for the product class) and warm-start production mode (validated recipe available). The two modes have different default policies for gate strictness, exploration budget, escalation thresholds, and evidence retention. There is no in-between "sort of known" state; mode is an explicit, auditable attribute of the run.

### P7 — Optimization Is Evidence-Backed
No worker downgrade, recipe promotion, context-budget reduction, topology change, retry-policy adjustment, or gate strictness change happens silently based on heuristics or intuition. Every optimization candidate is proposed by the Continual Optimizer, evaluated through a controlled experiment, promoted through the explicit Recipe Ladder, and auditable.

### P8 — No Silent Recipe Mutation
Production behavior (the configuration actually used to execute a product) never changes invisibly because of accumulated traces, background learning, or model drift. Every change to a recipe is an explicit versioned event: RecipeCandidate → Shadow → Canary → Validated, each step with experiment evidence, sign-off where required, and a rollback path.

### P9 — Exact Context Supplied to Workers Must Be Reproducible
For every node run, anyone can, using only stored manifests and the appropriate software commit, reconstruct the exact context package (files, contracts, handoffs, summaries, token count, formatting, ordering) that was sent to the worker. The reconstructed package matches a stored hash. Context is a controlled experimental variable, not an emergent property of a shared conversation.

### P10 — Parallelism Is Derived from DAG Dependencies
Parallel execution is not an arbitrary knob ("run 4 agents at once"). It is a structural consequence of the Product DAG: two nodes run concurrently if and only if the DAG declares them independent (no directed path between them). The scheduler computes the ready frontier; parallelism degree emerges, it is not preordained.

### P11 — Stronger Models Are Escalation Resources, Not Default Workers
Frontier-class models serve the orchestrator and the escalation ladder. The default assumption for every station is the Minimum Capable Worker (cheapest/smallest worker with sufficient evidence for that station/task class). Frontier models are called in only: when a weaker worker fails or is uncertain, for architectural decisions by the Configurator, for high-stakes reviewer gates, or during the initial exploration phase of a new product class.

### P12 — Quality Has Priority over Cost
The factory optimizes for cost, speed, and frontier-model usage only under the constraint that verified quality does not regress below a preregistered threshold. Any proposed optimization that trades quality for cost is rejected unless the Product Order explicitly accepts a lower quality tier.

---

## 3. Architectural Boundaries

The REDI Lab architecture is divided into four disjoint layers, with strict one-way or controlled dependencies between them. No layer may import types from a layer that is not its designated dependency.

```
┌────────────────────────────────────────────────────────────┐
│  D — Model-Based Judgment                                  │
│  (calls into C and A; never called directly by B)          │
├────────────────────────────────────────────────────────────┤
│  C — Deterministic Orchestration Logic                     │
│  (calls into A; calls into B through adapters;             │
│   delegates to D via bounded interfaces)                   │
├────────────────────────────────────────────────────────────┤
│  B — Provider / Runtime Adapters                           │
│  (implements interfaces defined by A;                     │
│   no layer depends on B's internal types)                  │
├────────────────────────────────────────────────────────────┤
│  A — REDI Method Domain Concepts                           │
│  (no external dependencies; pure typed contracts;          │
│   depended on by B, C, and D)                              │
└────────────────────────────────────────────────────────────┘
```

### Layer A — REDI Method Domain Concepts
- **Contents**: The pure, provider-agnostic domain objects and contracts that define what REDI Lab studies: ProductSpec, ProductClass, StationContract, WorkerProfile, TaskContract, ProductDAG, ContextManifest, HandoffManifest, GateResult, RunEvidence, ExperimentManifest, ProductRecipe, Event types, FailureTaxonomy, and all their schema versions.
- **Dependencies**: None. Layer A is a pure type/schema layer. It does not import anything from LLM SDKs, runtimes, databases, or I/O libraries.
- **Implementation note**: These types can be implemented as Pydantic models, dataclasses, JSON Schemas, Protobufs, or equivalent. The choice is an implementation detail as long as round-trip serialization, schema versioning, and no external-type leakage are preserved.

### Layer B — Provider / Runtime Adapters
- **Contents**: Implementations of interfaces defined by Layer A (and used by Layer C) that talk to external systems: ModelAdapter (wraps LLM APIs), WorkerRunner (executes a worker within a station), SandboxAdapter (isolated execution environment), TraceAdapter (captures execution traces from the runtime), GitAdapter, FileSystemAdapter, BenchmarkAdapter.
- **Dependencies**: Depends on Layer A (consumes and produces domain-typed artifacts). May depend on external SDKs (OpenAI SDK, Gemini SDK, Docker SDK, OpenAI Agents SDK, etc.) **internally only.** No adapter- or provider-specific type is ever exposed to Layer C or Layer D through the adapter's public interface.
- **Interface example**: `ModelAdapter.generate(prompt: ContextManifest, worker: WorkerProfile) -> ModelOutput` uses only Layer-A-typed inputs and outputs; the adapter translates internally to and from the provider's request/response format.
- **Rule**: If Layer B is removed and replaced with a different provider's or runtime's adapter set, Layers A, C, and D require zero code changes.

### Layer C — Deterministic Orchestration Logic
- **Contents**: The Scheduler, DAG compiler, deterministic Quality Gate Engine, Context Firewall, Event Router, Evidence Ledger writer, Recipe Store writer (promotion ladder logic), budget/retry policy enforcers, drift detectors, replay system, and the control-plane RBAC.
- **Dependencies**: Depends on Layer A. Interacts with Layer B **exclusively through the adapter interfaces defined in Layer A** (never directly through provider SDKs). Delegates to Layer D **exclusively through bounded, schema-validated judgment interfaces.**
- **Determinism requirement**: Given identical inputs (manifests, recipe versions, worker pools, budgets) and identical Layer B responses (captured during replay), a Layer C component produces bit-identical outputs or a documented, bounded divergence. Layer C has no access to randomness or current-time except through explicit, recorded parameters.

### Layer D — Model-Based Judgment
- **Contents**: The Factory Configurator / Orchestrator (Astra), independent model-based Reviewer gates, Context Compiler summarization heuristics where deterministic selection is insufficient, architectural-decision resolution, product-class novelty estimation.
- **Dependencies**: Depends on Layer A (reads and produces typed artifacts). May request information from Layer C through bounded read interfaces, but never writes directly to Layer C state—instead it emits typed decision events that Layer C processes deterministically.
- **Bounded scope**: Every invocation of Layer D is a scoped query with a typed input and a typed output schema. Layer D is never given a "do whatever you want" authority. The Orchestrator's outputs are DAG revisions, escalation approvals, recipe adaptation proposals, or novelty assessments—all typed.
- **Auditability**: Every Layer D invocation is recorded as an evidence entry: the exact input context, worker identity used, parameters, output, and timestamp. Layer D calls produce evidence just like worker nodes.

### Dependency Enforcement
- Layer A → none.
- Layer B → Layer A only.
- Layer C → Layer A only; Layer B only through adapter interfaces defined in A; Layer D only through bounded typed judgment interfaces.
- Layer D → Layer A only; Layer C only through bounded typed read APIs; Layer B only via the same adapter interfaces as Layer C (no direct provider access).

A static-analysis rule implemented at REDI-01 must reject any commit that introduces a cross-layer dependency violation.

---

## 4. Component Descriptions

This section describes each logical component, its responsibility, inputs/outputs, and which architectural layer it belongs to.

### 4.1 Product Intake (Layer C deterministic, delegates to D for novelty)
- **Purpose**: Transforms an external Product Order into a normalized ProductSpec, then classifies the order against known Product Classes and computes a novelty signal.
- **Inputs**: Raw product order (human-authored spec, requirements doc, issue tracker export, natural-language request), Product Class catalog.
- **Outputs**:
  - `ProductSpec` (Layer-A typed): normalized requirements, constraints, acceptance criteria, budget, risk level, required artifacts, explicit uncertainty markers for unknown requirements.
  - Product Class match (if any) with similarity score and compatibility flag.
  - `PRODUCT_ACCEPTED` event, or `PRODUCT_REJECTED` event with a reason code (ambiguous, out-of-scope, exceeds budget, legal issue).
- **Behavior**:
  - Deterministic parsing and normalization first; unknown fields are preserved but flagged, not silently guessed.
  - Class matching is deterministic (signature-based); if ambiguous, delegates to Layer D (Configurator) for novelty/judgment call, which is recorded as an `ARCHITECTURAL_DECISION_REQUIRED` event.
  - Preserves uncertainty: requirements that are "TBD" are modeled as TBD, not filled in with defaults.

### 4.2 ProductSpec (Layer A, pure type)
- **Definition**: The canonical internal representation of a product order, schema-versioned, with explicit fields for every requirement dimension including uncertainty.
- **Key fields**: `product_spec_id`, `schema_version`, `requirements[]`, `constraints[]`, `acceptance_criteria[]`, `budget_limit`, `risk_level`, `required_artifacts[]`, `explicitly_unknown_requirements[]`, `order_provenance`, `classification_tags[]`.
- **Rule**: ProductSpec is immutable once accepted. Any mid-run change produces a `SPEC_REVISED` event (a subclass of `ARCHITECTURAL_DECISION_REQUIRED`) and a new version; the old version is retained.

### 4.3 Product Class (Layer A, pure type)
- **Definition**: A reusable family signature for products with comparable production structure. Membership is determined by production-requirements similarity, not code similarity.
- **Key fields**: `product_class_id`, `schema_version`, `signature` (normalized structural features: station archetypes, DAG depth, typical artifact types, verification patterns), `compatibility_criteria`, `validated_recipes[]`, `evidence_coverage`.
- **Rule**: A ProductSpec is assigned to at most one primary Product Class. Transfer of a recipe from Class X to Class Y requires an explicit compatibility test; it never happens implicitly by tag matching.

### 4.4 Factory Configurator / Orchestrator (Layer D, model-based judgment)
- **Purpose**: The single high-level intelligence component in the factory. Handles all decisions that require product-level understanding or architectural judgment. It is NOT a routine execution engine.
- **Inputs**: ProductSpec, Product Class match (or novelty signal), Recipe Store state, active FactoryRun state, and designated decision events delegated from Layer C.
- **Outputs**: Typed decision artifacts, emitted as events for Layer C to apply:
  - Recipe selection / adaptation decision.
  - Initial ProductDAG (for unknown products) or DAG revision (when a structural problem is found).
  - Exploration budget allocation.
  - Architectural escalation approvals.
  - Recipe adaptation proposals.
  - Drift-response decisions (promote/retain/roll back).
- **Non-responsibilities**: Routine task execution; routine retry; reading every file produced; polling workers continuously. It is event-driven: it activates only when Layer C delegates a decision event to it, then produces its decision output and yields.
- **State model**: Holds management state only (product identity, DAG version, active escalations, budget status, risk flags, recipe version in use). It does not cache worker outputs, source files, or tool return values. Those live in workspaces and the Evidence Ledger; the Configurator requests them on demand through bounded Layer C read APIs when it needs them.

### 4.5 Product DAG (Layer A pure type + Layer C compiler)
- **Purpose**: Represents production as a dependency graph rather than a fixed chain. Parallelism, ordering, and interface boundaries are explicit graph properties.
- **Layer A DAG Type**: `ProductDAG` with `nodes: TaskContract[]` and `edges: (from_node_id, to_node_id)[]`. Schema-versioned. Every DAG revision increments the version.
- **Layer C DAG Compiler**: Deterministic algorithm that, given a ProductSpec + Recipe or Configurator output, produces the initial DAG. Runtime adaptations produce a new DAG version, never in-place mutation.
- **Node contents (TaskContract)**: station type, task contract text, prerequisites node IDs, outputs, downstream consumers, verification requirements, risk level, parallelizability flag, escalation policy ID, retry budget, cost budget, timeout budget.
- **Rule**: The DAG is versioned. Any change (node added/removed, edge added/removed, retry budget changed) must be recorded as a `DAG_REVISED` event with identity attribution and a linked DAG diff.

### 4.6 Scheduler (Layer C, fully deterministic)
- **Purpose**: Given the current ProductDAG version, the set of node statuses, and available execution capacity, compute the ready frontier and assign work.
- **Inputs**: Current DAG, per-node status (NOT_READY, READY, IN_PROGRESS, COMPLETED, FAILED, BLOCKED), available worker slots, resource capacity.
- **Outputs**: `NODE_READY` events for each node that transitions to READY (all prerequisites satisfied, no resource conflict). Scheduling decisions for which READY node to run next (if capacity is limited) are made by a deterministic priority rule recorded in the scheduler policy ID.
- **Determinism guarantee**: Given identical DAG, node statuses, and capacity, the scheduler produces identical ready sets and identical priority ordering.
- **Parallelism guarantee**: Two nodes are concurrently schedulable if and only if there is no directed path between them in the DAG. The scheduler never violates this.

### 4.7 Station Registry (Layer A registry + Layer C lookup logic)
- **Purpose**: Defines and enumerates all stable station roles in the factory.
- **Contents**: A registry of StationContracts, queryable by station type and eligible task classes.
- **Lookup rule**: Given a TaskContract's station type and task class, the registry returns exactly one active StationContract version. Version upgrades of a contract are explicit registry events; old versions are retained for replay.

### 4.8 Station Contract (Layer A, pure type)
- **Definition**: The canonical, versioned specification of a station's responsibility. StationContracts are provider-, model-, and runtime-agnostic.
- **Required fields**:
  - `station_contract_id`, `schema_version`.
  - `responsibility`: Plain-language description of what this station produces.
  - `accepted_inputs`: Typed schema of upstream HandoffManifest categories the station may receive.
  - `required_outputs`: Typed schema of artifacts this station must produce.
  - `allowed_tools`: Enum/names of tools this station may invoke. Tool names are logical (e.g., "FILE_EDIT", "PYTHON_RUN"), not provider-specific.
  - `owned_scope`: Paths, modules, or conceptual areas this station is permitted to modify.
  - `forbidden_scope`: Explicit disallowed areas.
  - `required_quality_gates[]`: Ordered list of gate IDs that must pass before the node can complete.
  - `compatible_task_classes[]`: Which task class tags this station can handle.
  - `candidate_worker_pool_id`: Which WorkerPool the station draws from.
  - `handoff_schema`: Schema version of the output HandoffManifest.
- **Rule**: A StationContract never mentions a model name, provider, tool SDK, or runtime.

### 4.9 Worker Registry (Layer A registry + Layer C lookup logic)
- **Purpose**: Enumerates all known candidate workers with their full identity and eligibility metadata.
- **Contents**: Registry of WorkerProfile records, versioned because provider behavior and model versions can change.
- **Lookup**: Given a WorkerPool ID and an optional filter (task difficulty, risk level, required tools), the registry returns eligible candidates in the pool's configured preference order.

### 4.10 Worker Pool (Layer A, pure type)
- **Definition**: A named set of workers eligible for assignment to a station, plus a preference/fallback ladder and per-tier selection policy.
- **Fields**: `worker_pool_id`, `schema_version`, `station_type`, `workers[]` (each with a WorkerProfile reference and a tier/preference rank), `selection_policy_id` (V0: deterministic ladder; future: evidence-weighted, contextual bandit), `fallback_ladder[]` (ordered escalation tiers when lower-tier workers fail), `exploration_budget` (fraction of runs reserved for non-top-tier workers to gather evidence).
- **Rule**: Worker pools are per-station-type. A worker can belong to multiple pools if its StationContract eligibility spans multiple stations.

### 4.11 Context Compiler / Context Firewall (Layer C deterministic + Layer D bounded judgment)
- **Purpose**: Constructs the exact, reproducible context package for a worker and enforces that nothing out-of-scope enters it.
- **Dual nature**:
  - **Context Firewall (Layer C, fully deterministic)**: Scope enforcement, allowlist checking, secret scanning, hash generation, manifest auditing. No judgment; purely rules. If a piece of content violates a rule, it is excluded and the exclusion is logged.
  - **Context Compiler (Layer C deterministic where possible; delegates to Layer D only when explicitly necessary)**: Assembles the package. Deterministic sub-steps: include task contract text, station contract text, product contract summary, all upstream HandoffManifests whose schema matches accepted_inputs, files explicitly listed in the task contract, and files matching deterministic path rules. When content selection is ambiguous (e.g., "which of 500 repo files are relevant to this station?"), the compiler either uses a deterministic-selection algorithm (with ID and parameters recorded) or delegates a single bounded "select relevant files" query to Layer D. In either case, the output is a deterministic ContextManifest: files are in a fixed order with hashes.
- **Output**: `ContextManifest` (Layer A) with all fields required by the Experiment Policy (Section 8): per-artifact identity, hashes, token counts, selection rationale codes, excluded-artifact list with rationale codes, and a deterministic `context_package_hash` of the assembled wire representation.
- **Rule**: The exact assembled context package must be byte-reproducible from the manifest plus the source data versions.

### 4.12 Execution Runtime Abstraction (Layer C interface; Layer B implementations)
- **Purpose**: Abstracts the mechanism that actually runs a worker node. The core logic (Layer C) defines an interface; specific runtimes (OpenAI Agents SDK, custom runtime, future local runtime) are implemented as adapters in Layer B.
- **Layer C Interface** (`WorkerRunner`):
  - `start_node_execution(task: TaskContract, station_contract: StationContract, worker: WorkerProfile, context: ContextManifest, sandbox: SandboxInstance) -> NodeExecutionHandle`
  - `poll(handle) -> NodeExecutionStatusUpdate`
  - `cancel(handle) -> void`
- **Handle / Status Update types are pure Layer A types**: `WORKER_STARTED`, `WORKER_COMPLETED`, intermediate events are emitted via the Event Router by the adapter, always translating internal runtime events to domain event types.
- **Rule**: No OpenAI-specific or runtime-specific type (e.g., `AgentRun`, `ToolCallResult`, `RunConfig`) crosses from Layer B into Layer C or D. All are translated before emission.

### 4.13 Sandbox Abstraction (Layer C interface; Layer B implementations)
- **Purpose**: Abstracts isolated workspace and execution environment.
- **Layer C Interface** (`SandboxAdapter`):
  - `create_instance(station_scope, network_policy, resource_limits) -> SandboxInstance`
  - `import_artifacts(instance, handoff_manifests[]) -> void`
  - `run_deterministic_gate(instance, gate_spec) -> GateResult`
  - `export_handoff(instance, output_schema) -> HandoffManifest`
  - `destroy(instance) -> void`
- **Properties**: Every node execution gets its own SandboxInstance (per the S-05, S-09, S-10 threat mitigations). No two nodes share mutable filesystem state. Network is denied by default; only enabled per station contract with a host allowlist. Resource limits are enforced.

### 4.14 Quality Gate Engine (Layer C deterministic gates; Layer D model-based reviewer gates)
- **Purpose**: The layered verification subsystem that evaluates node outputs before a node can complete.
- **Gate types, executed in order (deterministic first — P3)**:
  1. **Deterministic gates (Layer C, fully deterministic, no model)**:
     - Build/compile.
     - Lint / type checks.
     - Unit tests.
     - Integration tests.
     - Schema validation (HandoffManifest against schema).
     - Contract tests (outputs match StationContract's required_outputs schema).
     - Static analysis / security scanners (with preregistered rule sets).
     - Allowed-file / scope checks (only owned_scope files modified, no forbidden_scope touched).
     - Secret scans (on both diffs and whole outputs).
     - Migration / compatibility checks (if applicable).
     - Reproducibility checks (e.g., the build is deterministic).
  2. **Model-based gates (Layer D bounded judgment)**:
     - Invoked only after deterministic gates pass or explicitly record expected-failure categories.
     - The reviewer worker receives: the artifact under review, the gate criteria (from the ProductSpec acceptance_criteria and StationContract review requirements), and relevant upstream HandoffManifests. It does NOT receive the builder's private reasoning trace, tool logs, or input context.
     - The reviewer produces a typed `GateResult` with pass/fail, category-specific scores, and structured feedback. The result conforms to a schema; free-form text is optional and secondary to typed fields.
- **Output events**: `GATE_PASSED` (all gates passed), `GATE_FAILED` (at least one gate failed). Every gate result is individually recorded in the RunEvidence with its type, criteria, and reviewer identity if model-based.
- **Rule (P2 — Independent Verification)**: A single model trajectory cannot be both the producer of an output AND the sole reviewer of that output. The reviewer (if model-based) must use a separate WorkerProfile execution from the builder, with a separate context. Self-review may be included as an additional data point in evidence but never counts toward the pass/fail decision.

### 4.15 Handoff Manifest (Layer A, pure type)
- **Definition**: The structured, schema-versioned artifact that crosses station boundaries. A station's output is not "code in a folder" or "a chat message"; it is a HandoffManifest.
- **Required fields**: `handoff_manifest_id`, `schema_version`, `from_station_type`, `from_node_id`, `to_station_types[]` (or broadcast flag), `artifacts[]` (each with path, content hash, size, type, description), `interface_declarations[]` (typed API contracts, function signatures, schema definitions downstream consumers need), `known_issues[]`, `assumptions_made[]`, `gate_results[]` (references, not inline full logs), `downstream_todo_hints[]` (optional, structured).
- **Rule**: Downstream nodes never access a sibling's workspace directly. All cross-station data flow goes through HandoffManifest import via the Sandbox Adapter's `import_artifacts` method, which validates schema and scope before writing anything into the downstream workspace. HandoffManifests are the only inter-station communication channel (P1 — Contracts over Conversations).

### 4.16 Evidence Ledger (Layer C, append-only writer; Layer A types)
- **Purpose**: The immutable append-only research backbone. Every node attempt, gate result, event, and decision produces a normalized RunEvidence entry.
- **Write model**: Only Layer C components and Layer D decision recorders write to the ledger, through a single writer API that enforces append-only semantics and schema validation. No component, including the Configurator, can edit or delete an existing ledger row. A row can be marked `invalidated` with an invalidation record (reason + identity), but the original content is retained.
- **RunEvidence contents**: Defined in Research Plan Section 8.9 and in the Experiment Policy. Every field required by those documents is present in the Layer-A RunEvidence schema.
- **Raw-trace separation**: Full low-level execution traces (Layer B's runtime logs) are stored separately from the normalized ledger. They are linked by run ID and available for deep audit, but analyses default to the normalized fields (Data Policy Section 3, D08 vs D09).

### 4.17 Product Recipe Store (Layer C promotion-ladder logic; Layer A types)
- **Purpose**: Versioned persistence of ProductRecipes, with the explicit promote/retain/rollback ladder.
- **Recipe contents**: As defined in the Research Plan Section 8.10 and Research Charter Terminology.
- **Ladder (Experiment Policy Section 19.6)** enforced by Layer C logic:
  1. **Candidate** — generated by Recipe Learner from evidence. Not used for production mode.
  2. **Shadow** — executed alongside validated recipe on a fraction of runs; outputs compared but not accepted.
  3. **Canary** — used for a bounded fraction of real runs with elevated monitoring; automatic rollback on regression.
  4. **Validated** — used for standard production mode.
- **Events**: `RECIPE_CANDIDATE_CREATED` (Recipe Learner emits via Layer C), `RECIPE_PROMOTED` (promotion through a rung), `RECIPE_ROLLED_BACK`, `RECIPE_REVALIDATED`.
- **Rule (P8 — No Silent Mutation)**: Production behavior for a class always uses the current Validated recipe. A recipe version is referenced explicitly by ID in the FactoryRun record. Recipes never change content under the same ID; every change is a new version with a promotion event.

### 4.18 Continual Optimizer (Layer C experiment proposal logic + Layer D candidate analysis)
- **Purpose**: Generates optimization candidates, schedules controlled experiments to evaluate them, and recommends promotions or rollbacks. It proposes; it does not mutate production policy directly.
- **Candidate types**:
  - Worker downshift (smaller/cheaper worker in a station).
  - Different provider for a station.
  - Lower reasoning level in a worker.
  - Smaller context budget or different context selection strategy.
  - Verification depth adjustment (add or remove a gate tier).
  - New parallel branch in the DAG template.
  - Retry/escalation threshold adjustment.
  - New station decomposition (split or merge a station type).
- **Flow**: Optimizer (Layer C deterministic scheduling) proposes candidate → creates ExperimentManifest → experiment executes → results recorded in Evidence Ledger → promotion decision per Experiment Policy Sections 19/20 → Recipe Store applies ladder transition.
- **Rule (P7 — Evidence-Backed)**: No candidate enters the Shadow rung or higher without a completed experiment cited. No "trust me, this should be better" promotions.

### 4.19 Drift / Revalidation Subsystem (Layer C detection + Layer D scope judgment)
- **Purpose**: Detect model/provider/environment/pricing drift and orchestrate targeted revalidation before recipes silently degrade.
- **Detection (Layer C, fully statistical/deterministic)**:
  - Periodic calibration runs on a held-out standard task set; comparison against historical baselines using control-chart methods with preregistered thresholds.
  - Event triggers: model version change notifications from providers, pricing schedule updates, environment manifest hash changes, REDI Lab version bumps, new worker model availability.
  - Recipe-performance monitoring: statistically significant degradation on validated-recipe production runs.
- **Scope judgment (Layer D, bounded)**: When drift is detected, the subsystem asks the Configurator: which stations and recipe fields are plausibly affected by this drift event? The answer narrows the revalidation scope instead of revalidating everything.
- **Procedure**: Follows Experiment Policy Section 21. Outputs `DRIFT_DETECTED`, revalidation experiment records, and eventually promote/retain/rollback decisions applied to the Recipe Store.

### 4.20 Event Router (Layer C, fully deterministic)
- **Purpose**: The single nervous system of the factory. Every lifecycle event passes through the Event Router.
- **Responsibilities**:
  - Validates event schema (Layer A Event types) on ingest.
  - Validates event scope/emitter identity: e.g., a worker sandbox can emit WORKER_COMPLETED only for the node ID matching its scoped token; an event claiming to be GATE_PASSED but signed by a builder identity is rejected. (Threat model S-07 mitigation.)
  - Determines the event's routing destination: deterministic Layer C handler, Evidence Ledger writer, delegated-to-Layer-D decision queue, or external subscriber.
  - Maintains the event ordering guarantee (deterministic total order consistent with per-node partial order).
- **Outputs**: Events are delivered to handlers, written to ledger, and optionally broadcast to external subscribers via the adapter layer.

### 4.21 Replay System (Layer C, fully deterministic)
- **Purpose**: Given historical evidence manifests, reconstruct the factory state and replay events to reproduce results, validate the harness, or audit claims.
- **Inputs**: FactoryRun ID (or ExperimentManifest ID + run IDs), target software commit, target environment manifest, stored ContextManifests, stored WorkerProfile versions, stored recipe versions, raw traces (optional, for full-depth replay).
- **Outputs**: Replay pass/fail (did replayed gate results, node statuses, and final product status match the stored record within preregistered tolerance?), replay diff report, replay-level metrics.
- **Determinism requirement (Experiment Policy Section 5)**: Layer C must be fully replayable. Layer B (providers, sandboxes) may have documented non-determinism tolerances; those are recorded, and the replay check flags any divergence beyond the tolerance.

---

## 5. Event Model

### 5.1 Lifecycle Events (Normative Set)

The following events are the normative lifecycle vocabulary of REDI Lab. All components communicate via these events. Additional events may be added for implementation convenience (e.g., internal telemetry), but the core product execution flow uses only these.

#### Product Lifecycle
| Event ID | Emitted By (Layer) | Payload | Deterministic Handler (Layer C) | When Orchestrator (D) May Activate |
|---|---|---|---|---|
| `PRODUCT_ACCEPTED` | Product Intake (C) | `product_spec_id`, `product_class_id` (optional), `novelty_signal`, `budget`, `risk` | Initialize FactoryRun; create empty ProductDAG placeholder; route to Configurator for plan compilation if novelty > threshold. | **Always** — this triggers the initial plan/reuse decision. |
| `PLAN_COMPILED` | Configurator (D) via C | `product_dag_id:version`, `recipe_id` (optional, if reused), `mode` (explore/produce), `exploration_budget` | Persist DAG version; hand to Scheduler for initial ready-frontier compute. | No further activation needed unless DAG revision is required later. |
| `MILESTONE_COMPLETED` | Scheduler / Gate Engine (C) | `milestone_id`, `nodes_completed[]`, `remaining_risk` | Check if milestone triggers any policy; no-op in V0 beyond logging. | Only if milestone completion was a prerequisite for a delegated decision. |
| `INTEGRATION_FAILED` | Integration Station Gate (C) | `node_id`, `conflict_report`, `involved_branches[]` | Apply retry budget for integration node; if exceeded, escalate. | **Activates** if retries exhausted — architectural conflict / DAG revision decision. |
| `PRODUCT_VERIFIED` | Final Acceptance Gate Engine (C) | `product_spec_id`, `factory_run_id`, `final_gate_results[]`, `total_cost`, `total_tokens`, `wall_clock` | Persist final product outcome; signal Evidence Ledger to close the run; signal Recipe Learner to generate candidates if criteria met. | **Activates** only if mode was Exploration and Configurator must review whether to create a recipe; otherwise logs and closes. |

#### Node / Worker Lifecycle
| Event ID | Emitted By (Layer) | Payload | Deterministic Handler (Layer C) | When Orchestrator (D) May Activate |
|---|---|---|---|---|
| `NODE_READY` | Scheduler (C) | `node_id`, `task_contract_id`, `elapsed_vs_budget` | Select worker from pool per policy; hand to Execution Runtime; emit `WORKER_STARTED`. | Never for standard nodes; only if node policy marks this node as requiring Configurator-level worker choice. |
| `WORKER_STARTED` | Execution Runtime (C/B boundary) | `node_id`, `worker_id`, `context_manifest_hash`, `sandbox_id` | Record in Evidence Ledger; start timeout/cost budget clocks. | Never. |
| `WORKER_COMPLETED` | Execution Runtime (C/B boundary) | `node_id`, `worker_id`, `output_handoff_id`, `raw_trace_ref`, `token_counts`, `elapsed` | Record in Evidence Ledger; pass output to Quality Gate Engine; stop budget clocks. | Never. |
| `GATE_PASSED` | Quality Gate Engine (C) | `node_id`, `gate_results[]` | Update node status to COMPLETED; export HandoffManifest; update DAG status (prerequisite-satisfied flags); trigger Scheduler recompute. If integration node, run integration checks. | Never unless DAG recompute surfaces a structural issue. |
| `GATE_FAILED` | Quality Gate Engine (C) | `node_id`, `failing_gates[]`, `failure_taxonomy_code`, `retry_budget_remaining` | Classify failure per taxonomy. Apply retry policy: `RETRY_REQUESTED`, `CONTEXT_REBUILD_REQUIRED`, `WORKER_ESCALATION_REQUIRED`, or escalate to architectural. | Never if deterministic policy covers the failure class; only if policy says so. |
| `NODE_COMPLETED` | Scheduler / Gate Engine (C aggregate) | `node_id`, `total_attempts[]`, `final_worker_id` | Aggregate ledger records; confirm handoff exported; recompute Scheduler frontier. | Never. |

#### Retry / Escalation Sub-Lifecycle
| Event ID | Emitted By (Layer) | Payload | Deterministic Handler (Layer C) | When Orchestrator (D) May Activate |
|---|---|---|---|---|
| `RETRY_REQUESTED` | Gate Engine (C) after GATE_FAILED | `node_id`, `original_attempt_id`, `failure_taxonomy_code=transient_or_local`, `retry_index`, `new_budget_remaining` | Resubmit node to Execution Runtime with same worker, same context (unless context-rebuild flag is set by a different event). | Never. |
| `CONTEXT_REBUILD_REQUIRED` | Gate Engine (C) or Retry Policy (C) | `node_id`, `reason_code=context_insufficient_or_contaminated`, `attempts_before` | Call Context Compiler to produce a **new ContextManifest version** (excluded-artifact list expanded, or additional relevant sources pulled in). Resubmit with new context, same worker. | Rarely: only if context rebuild policy requires identifying *why* context was insufficient and requires LLM judgment, in which case delegated. |
| `WORKER_ESCALATION_REQUIRED` | Gate Engine (C) / Retry Policy (C) | `node_id`, `failure_taxonomy_code=capability_insufficient_or_uncertain`, `current_worker_tier`, `escalation_target_tier` | Select next worker up the station's fallback ladder; optionally recompile context for the stronger worker's larger budget. Resubmit. | Rarely: only if the escalation ladder is exhausted and no stronger worker exists in the pool, which triggers architectural. |
| `ARCHITECTURAL_DECISION_REQUIRED` | Retry/Escalation Policy (C) or Scheduler (C) | `node_id`, `trigger_event_id`, `reason_code` (architectural conflict, exhausted escalation ladder, integration failure, budget exceeded, DAG contradiction, scope expansion request, novelty threshold, drift response required) | Pause node; queue decision for Configurator; record state as BLOCKED_PENDING_DECISION. | **Always** — this event is the explicit delegation. Configurator must produce a typed decision: DAG revision, worker pool override, spec revision, scope approval/denial, budget override, or node abandonment. Decision is applied by Layer C as a new event. |

#### Recipe / Optimization Lifecycle
| Event ID | Emitted By (Layer) | Payload | Deterministic Handler (Layer C) | When Orchestrator (D) May Activate |
|---|---|---|---|---|
| `RECIPE_CANDIDATE_CREATED` | Recipe Learner (C) after PRODUCT_VERIFIED | `product_class_id`, `candidate_recipe_id:version`, `supporting_run_ids[]` | Transition candidate to CANDIDATE rung in Recipe Store; register for shadow runs on next exploration-budget slots. | Optional: Configurator reviews if the candidate is structurally sensible before shadow entry. |
| `RECIPE_PROMOTED` | Promotion Logic (C) after ladder criteria met | `product_class_id`, `recipe_id`, `from_rung`, `to_rung`, `supporting_experiment_ids[]` | Apply promotion; update Validated pointer if rungs reached Validated; log rollback path. | Optional: Configurator signs off for promotion to Validated (per Experiment Policy Section 19 sign-off requirements). |
| `DRIFT_DETECTED` | Drift Subsystem Detector (C) | `drift_type`, `affected_component_ids[]`, `baseline_vs_current_statistics`, `confidence` | Freeze automatic promotions for the affected scope; spawn targeted revalidation experiment; record revalidation-scope proposal. | **Activates**: Configurator narrows revalidation scope (optional Layer D judgment); otherwise default scope is all affected stations. |

### 5.2 Deterministic vs. Orchestrator-Handling Summary

The table above encodes the following rule:

**Events handled by default fully deterministically (no Orchestrator activation required unless explicitly overridden by policy):**
`NODE_READY`, `WORKER_STARTED`, `WORKER_COMPLETED`, `GATE_PASSED`, `NODE_COMPLETED`, `RETRY_REQUESTED`, `MILESTONE_COMPLETED`, `RECIPE_CANDIDATE_CREATED`, `PLAN_COMPILED` handler (after DAG is produced), and most paths of `GATE_FAILED` that map to `RETRY_REQUESTED` or `WORKER_ESCALATION_REQUIRED`.

**Events that always or conditionally delegate to the Orchestrator / Configurator / Layer D judgment:**
`PRODUCT_ACCEPTED` (initial plan/reuse), `ARCHITECTURAL_DECISION_REQUIRED` (any class), `INTEGRATION_FAILED` (if retries exhausted), `PRODUCT_VERIFIED` (if exploration mode → candidate recipe review), `RECIPE_PROMOTED` (Validated rung sign-off), `DRIFT_DETECTED` (scope narrowing), and conditional paths of `CONTEXT_REBUILD_REQUIRED` / `WORKER_ESCALATION_REQUIRED` if their ladders/policies are exhausted.

---

## 6. Component-to-Layer Reference Card

| Component | Primary Layer | Secondary Layer (Judgment Delegation) |
|---|---|---|
| Product Intake | C (deterministic parsing/classification) | D (novelty/class ambiguity) |
| ProductSpec | A | — |
| Product Class | A | — |
| Factory Configurator / Orchestrator | D | — |
| Product DAG (type) | A | — |
| Product DAG Compiler | C | D (unknown-product initial plan) |
| Scheduler | C | — |
| Station Registry | A + C lookup | — |
| Station Contract | A | — |
| Worker Registry | A + C lookup | — |
| Worker Pool | A | — |
| Context Firewall (enforcement) | C | — |
| Context Compiler (assembly) | C | D (ambiguous file selection only) |
| Execution Runtime (interface) | C interface | — |
| Execution Runtime (implementation) | B adapters | — |
| Sandbox (interface) | C interface | — |
| Sandbox (implementation) | B adapters | — |
| Quality Gate Engine — deterministic gates | C | — |
| Quality Gate Engine — model-based reviewer gates | D | — |
| Handoff Manifest | A | — |
| Evidence Ledger (writer) | C + A types | — |
| Product Recipe (type) | A | — |
| Recipe Store (promotion ladder) | C + A types | D (Validated sign-off) |
| Continual Optimizer (proposal/scheduling) | C | D (optional candidate ranking) |
| Drift Subsystem — detection | C | — |
| Drift Subsystem — scope judgment | — | D |
| Event Router | C | — |
| Replay System | C | — |

---

## 7. Change Control

This Logical Architecture document is frozen at REDI-00A with version 1.0.

Substantive changes to components, event definitions, boundary rules, or principles require:
1. A documented rationale citing specific evidence, identified logical flaw, or newly discovered threat.
2. Cross-review against the Research Charter, Experiment Policy, Data Policy, and Threat Model to ensure consistency. Changes that violate any architectural principle (Section 2) must either revise the principle (a larger change requiring explicit justification) or be rejected.
3. Version increment and a dated changelog entry appended to a future version of this document.
4. Replay of historical evidence affected by any behavioral change: if an event definition changes meaning, any stored events of that type must either be re-mapped to the new semantics with a recorded transformation, or be annotated as legacy-semantics so that analyses do not mix them.
