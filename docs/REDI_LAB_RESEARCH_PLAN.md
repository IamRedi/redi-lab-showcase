REDI LAB

Adaptive Self-Optimizing AI Software Factory Research

REDI Method — Research & Architecture Plan — v0.2

Date: 2026-09-15
Status: Research baseline / repo foundation
Primary objective: Build and experimentally validate a software-production system that learns the minimum model capability, context, workflow configuration, verification policy, and cost required to produce verified software products.

1. Executive Summary

REDI Lab is not another generic multi-agent framework, coding assistant, or static software-factory workflow.

REDI Lab is a research control plane for adaptive software production.

The system treats software development as a production process composed of specialized stations. Each station has a stable responsibility and contract, but may have multiple candidate workers (different models, providers, reasoning levels, or later local models). A strong orchestrator receives the product order, creates or selects a production graph, assigns work to stations, and intervenes only when judgment is required.

For a new product class, REDI Lab operates in exploration mode:

decompose the product into a dependency graph;

assign each task to an appropriate station;

try candidate workers under controlled policies;

independently verify outputs at quality gates;

escalate only when weaker workers fail or uncertainty exceeds policy;

record every run as structured evidence;

derive a versioned Product Recipe from successful production;

reuse the recipe for future products of the same class;

continuously test whether cheaper/smaller workers, smaller context, or better parallelization can preserve quality.

For a known product class, REDI Lab operates in production mode:

warm-start from the best known recipe;

execute independent tasks in parallel when dependency constraints allow;

use the lowest-cost worker with sufficient evidence;

preserve independent verification;

reserve a small controlled exploration budget for detecting better configurations;

detect degradation, model drift, or changed requirements and trigger re-optimization.

The long-term research target is not simply "which model should handle this prompt?" It is:

What is the minimum production organization required to produce this class of software at a verified quality level?

2. What REDI Lab Is — and Is Not

REDI Lab is

an adaptive production control plane;

a research laboratory for agentic software engineering;

a system for learning product-class production recipes;

a system for discovering the Minimum Capable Worker for each station/task class;

a context-isolation and context-budget experiment platform;

a quality-gated execution engine;

a provider- and model-independent evidence system;

a replayable benchmark environment;

a future foundation for a commercial developer platform.

REDI Lab is not

a replacement for OpenAI Agents SDK, Codex, Gemini tooling, LangGraph, CrewAI, or similar runtimes;

a static chain such as Planner → Coder → Reviewer;

a collection of agents sharing one giant conversation;

a model router whose only job is choosing a cheap or expensive LLM;

a chat-based "software company" simulation;

a model-training project in its first phase;

a SaaS/dashboard project in its first phase.

Existing runtimes already solve much of agent execution, tools, sandboxes, handoffs, sessions, and tracing. REDI Lab should reuse those capabilities behind adapters rather than rebuilding them.

3. Research Positioning

The following ideas already exist publicly and therefore must not be claimed as REDI Lab's innovation by themselves:

multi-agent role specialization;

assembly-line software development;

orchestrator/worker architectures;

independent validators;

model routing based on price/capability;

routing based on partial execution trajectories;

reusable workflow memory;

dynamic role assignment;

software-factory quality gates;

long-running agent harnesses;

agent tracing and sandbox execution.

REDI Lab's research differentiation is the combined production-level learning problem:

Core differentiator

Given a product class, discover, store, reuse, and continuously improve the entire production configuration — workflow graph, station roles, worker assignment, context allocation, verification policy, escalation policy, and parallelism — using evidence from prior verified production.

This is more specific than task routing and more structural than workflow memory.

4. Research Thesis

Primary thesis

A product-class-aware software factory can achieve equal or better verified product quality with lower frontier-model usage, lower context usage, lower cost, and/or lower wall-clock time than a monolithic frontier agent or a fixed multi-agent workflow by learning and reusing production recipes from verified execution evidence.

Research hypotheses

H1 — Station specialization

Stable role contracts allow weaker/cheaper workers to perform bounded work reliably compared with generalist execution.

H2 — Context isolation

Role-specific context selected by contract can equal or outperform larger generic project context while reducing token use and context contamination.

H3 — Minimum Capable Worker

For many recurring station/task classes, there exists a worker below the frontier model whose verified performance is statistically non-inferior for that bounded work.

H4 — Independent quality gates

Separating production from verification reduces false-success rates compared with self-verification by the same execution trajectory.

H5 — Product Recipe learning

Verified executions can be transformed into reusable production recipes that reduce discovery cost for future products of the same class.

H6 — Warm-start production

A validated recipe should reach stable production quality faster and with fewer frontier-model interventions than cold-start planning.

H7 — Continuous downshifting

The factory can periodically test smaller/cheaper workers and adopt them when evidence shows quality is preserved.

H8 — Adaptive factory topology

Different product classes benefit from different DAG structures, station combinations, verification depth, and parallelism; therefore a single fixed workflow is suboptimal.

H9 — Production evidence beats model reputation

Historical station-specific evidence should become more useful than global model rankings for choosing workers inside the factory.

5. Key Research Terms

Product Class

A reusable family of products with comparable production structure, e.g.:

business website;

CRUD SaaS;

REST API service;

CLI tool;

content platform;

data-processing utility.

Product classification must not mean "same codebase." It means sufficiently similar production requirements to justify transfer of a recipe.

Station

A stable production responsibility, not a model.

Examples:

architecture;

data/schema;

backend/API;

frontend/UI;

integration;

test generation;

security verification;

review;

packaging/release.

Worker

A model/runtime configuration capable of operating a station.

A Worker identity includes more than a model name:

provider;

model/version;

reasoning level;

tool permissions;

context limit/budget;

execution policy;

cost profile.

Worker Pool

The candidate workers eligible for a station.

Quality Gate

An independent decision boundary with deterministic tests where possible and model-based judgment only where necessary.

Product Recipe

A versioned, evidence-backed production configuration learned for a product class.

Evidence

Structured observations from a run, not free-form chat history.

Minimum Capable Worker (MCW)

The least expensive/least capable tested worker that meets the predefined quality criterion for a bounded station/task class with sufficient evidence.

This is a measured operational concept, not a claim that model intelligence can be expressed as a percentage.

6. Design Principles

Contracts over conversations.
Inter-agent communication is structured through artifacts and state transitions, not shared chat history.

Roles are stable; models are replaceable.
Station identity must never depend on a specific provider.

Verification is independent.
A worker cannot be the sole authority that its own output is correct.

Deterministic logic before LLM judgment.
Tests, schemas, dependency rules, budgets, and retry policies are software decisions wherever possible.

Orchestrator sees management state, not full implementation history.
Astra should know what is being produced, dependencies, status, risks, and escalation requests. It should not accumulate every implementation detail.

Unknown products are explored; known products are exploited.
The system must explicitly distinguish cold-start discovery from warm-start production.

Every optimization must be evidence-backed.
No worker downgrade, recipe promotion, or gate weakening based only on intuition.

Replayability is mandatory.
An experiment result without reproducible manifest, versions, input, and outcome is not valid research evidence.

Quality dominates cost.
REDI Lab optimizes cost only under a defined quality constraint.

The factory must survive model turnover.
New models should enter a worker pool and be evaluated without redesigning the factory.

7. High-Level Architecture

                         PRODUCT ORDER
                              │
                              ▼
                      PRODUCT INTAKE
                              │
                 ProductSpec + constraints
                              │
                              ▼
                  FACTORY CONFIGURATOR
                 (strong orchestrator)
                              │
             ┌────────────────┴────────────────┐
             │                                 │
       known product?                    unknown/novel?
             │                                 │
             ▼                                 ▼
       RECIPE STORE                      EXPLORATION PLAN
             │                                 │
             └────────────────┬────────────────┘
                              ▼
                       PRODUCT DAG
                              │
                    eligible task frontier
                              │
             ┌────────────────┼─────────────────┐
             ▼                ▼                 ▼
         STATION A        STATION B         STATION C
             │                │                 │
       Worker Pool        Worker Pool       Worker Pool
             │                │                 │
             ▼                ▼                 ▼
         EXECUTION        EXECUTION         EXECUTION
             │                │                 │
             ▼                ▼                 ▼
          GATE A           GATE B            GATE C
             │                │                 │
             └────────────────┼─────────────────┘
                              ▼
                         INTEGRATION
                              │
                         SYSTEM GATES
                              │
                              ▼
                      FINAL ACCEPTANCE
                              │
                              ▼
                       EVIDENCE LEDGER
                              │
              ┌───────────────┴────────────────┐
              ▼                                ▼
        RECIPE LEARNER                 PERFORMANCE MODEL
              │                                │
              └───────────────┬────────────────┘
                              ▼
                    CONTINUAL OPTIMIZER
                              │
                     next production run

8. Core Components

8.1 Product Intake

Purpose:

convert a user order into a normalized ProductSpec;

capture requirements, constraints, acceptance criteria, budget, risk level, and required artifacts;

classify similarity to known product classes;

calculate a novelty signal.

The intake must preserve uncertainty. Unknown requirements must remain explicitly unknown rather than being silently guessed.

8.2 Factory Configurator

Primary high-level intelligence layer.

Responsibilities:

determine whether a known recipe can be reused;

decide whether recipe adaptation or cold-start discovery is required;

compile the initial Product DAG;

select required stations;

set gate strictness;

allocate exploration budget;

handle architectural escalations.

Non-responsibilities:

routine implementation;

routine test fixing;

reading every file modified by workers;

polling workers continuously.

The Configurator is event-driven and reactivated only when a policy or event requires high-level judgment.

8.3 Product DAG Compiler

Represents production as dependencies rather than a fixed linear chain.

Each node contains:

station type;

task contract;

prerequisites;

outputs;

downstream consumers;

verification requirements;

risk level;

parallelizability;

escalation policy.

The scheduler may run independent ready nodes concurrently.

The DAG is versioned. Runtime adaptation must be recorded as a graph revision rather than silently mutating the plan.

8.4 Station Registry

Defines stable roles.

Each StationContract specifies:

responsibility;

accepted inputs;

required outputs;

allowed tools;

owned scope;

forbidden scope;

required quality gates;

compatible task classes;

candidate worker pool;

handoff schema.

Stations are domain concepts. They must not contain OpenAI-, Gemini-, or model-specific assumptions.

8.5 Worker Registry

Each worker profile records:

provider;

model identifier and version;

runtime adapter;

reasoning configuration;

cost;

context limits;

latency observations;

tool support;

structured-output support;

station eligibility;

historical evidence;

reliability/confidence;

current availability.

Worker profiles are versioned because provider behavior and model versions can change.

8.6 Context Compiler / Context Firewall

This is a first-class research component.

Its purpose is not merely summarization. It constructs a bounded context package from:

Task Contract;

Station Contract;

Product Contract;

relevant source files;

required interfaces;

relevant prior decisions;

upstream handoff artifacts;

known failure evidence.

It excludes unrelated subsystem history by default.

Every context package must be reproducible and hashed.

Research variables include:

full repository vs selected context;

token budget;

file selection strategy;

summary vs original artifacts;

previous failures included/excluded;

cross-station information exposure.

8.7 Execution Engine

Responsibilities:

launch workers;

provide isolated workspace;

apply tool/network permissions;

persist run state;

enforce timeout/budget limits;

capture outputs;

emit lifecycle events.

The V0 runtime may use OpenAI Agents SDK and its sandbox/tracing capabilities behind REDI Lab adapters. Core REDI Method contracts must not depend on that runtime.

8.8 Quality Gate Engine

Quality gates are layered.

Deterministic gates

Preferred whenever possible:

build/compile;

lint/type checks;

unit tests;

integration tests;

schema validation;

contract tests;

static analysis;

allowed-file/scope checks;

secret scans;

migration checks;

reproducibility checks.

Model-based gates

Used for requirements that cannot be fully deterministic:

requirement completeness;

architecture consistency;

UX/design review;

ambiguous behavior;

code-review judgment.

A model-based reviewer must receive the artifact, criteria, and relevant context — not the builder's private reasoning trajectory.

8.9 Evidence Ledger

The Evidence Ledger is the research backbone.

Every attempt records at minimum:

run ID;

experiment ID;

product/product class;

recipe/version;

DAG/node/station;

task class;

worker profile/version;

context manifest/hash;

input/output token counts;

reasoning configuration;

execution time;

queue/wall-clock time;

files read;

files changed;

tests executed/results;

deterministic gate results;

reviewer result;

retry count;

escalation;

human intervention;

final status;

cost;

failure taxonomy;

source revision/commit;

environment manifest.

Raw traces may be stored separately from normalized evidence.

8.10 Recipe Store

A ProductRecipe is not a prompt template.

It contains:

product-class signature;

required stations;

DAG template;

task decomposition patterns;

station worker preferences;

fallback ladders;

context strategies and budgets;

verification policies;

retry policies;

escalation policies;

parallelization rules;

expected quality/cost/latency distributions;

confidence/evidence coverage;

validity conditions;

recipe version;

last revalidation state.

Recipes are promoted only from evidence.

8.11 Continual Optimizer

The optimizer proposes changes; it does not silently rewrite production policy.

Candidate optimizations include:

smaller/cheaper worker;

different provider;

lower reasoning level;

smaller context budget;

alternate context selection;

reduced/increased verification depth;

new parallel branch;

changed retry threshold;

changed escalation threshold;

new station decomposition.

Each proposed change is evaluated under a controlled experiment before recipe promotion.

9. Operating Modes

Mode A — Cold-Start / Unknown Product

Triggered when no recipe has sufficient similarity/confidence.

Behavior:

normalize ProductSpec;

produce conservative DAG;

use stricter quality gates;

use broader candidate worker exploration;

preserve more evidence;

allow escalation earlier;

generate first recipe only after final verification.

No single successful run is sufficient to declare a stable recipe.

Mode B — Warm-Start / Known Product

Triggered when a compatible validated recipe exists.

Behavior:

load recipe;

adapt variable product requirements;

launch eligible parallel nodes;

use evidence-preferred workers;

keep normal gates;

reserve a bounded exploration fraction;

compare production outcome with recipe expectations.

Mode C — Drift/Revalidation

Triggered by:

model version change;

provider behavior change;

repeated quality degradation;

cost/latency shift;

changed tool/runtime;

changed product requirements;

new stronger/cheaper candidate model.

Behavior:

freeze automatic recipe promotion;

run targeted revalidation;

compare against current recipe;

promote, retain, or roll back.

Mode D — New Worker Onboarding

A new model is not trusted globally.

It enters specific station pools as a candidate and is evaluated on:

historical replay tasks;

shadow runs;

bounded live exploration;

production eligibility only after evidence threshold.

10. Worker Selection Strategy

V0

No learned router.

Use deterministic policies so experiments remain interpretable:

station eligibility;

task risk;

historical verified success;

cost;

latency;

context/tool compatibility;

fallback ladder.

Later

Evaluate contextual bandit / probabilistic selection using evidence features.

The optimizer must distinguish:

exploitation: use current best-known worker;

exploration: gather information about alternatives.

Exploration has a cost and must have an explicit budget.

Parallel candidate execution is permitted only when:

product is novel/high-value;

uncertainty is high;

expected information value justifies added cost;

outputs can be compared independently;

no unsafe side effects occur.

11. Failure and Escalation Model

All failures require a taxonomy.

Minimum categories:

specification ambiguity;

context insufficiency;

context contamination;

capability insufficiency;

tool failure;

environment failure;

dependency mismatch;

scope violation;

deterministic test failure;

reviewer rejection;

integration conflict;

security violation;

timeout/budget exhaustion;

orchestrator planning error.

Escalation is based on failure class, not only retry count.

Example:

cheap worker
   │
   ├─ deterministic local error → bounded retry
   │
   ├─ insufficient context → rebuild context, retry
   │
   ├─ capability suspected → stronger worker
   │
   └─ architectural conflict → orchestrator

This distinction is essential for learning. Otherwise every failure incorrectly teaches the factory that the worker was too weak.

12. Orchestrator Event Model

The orchestrator is not continuously active.

Core events:

PRODUCT_ACCEPTED

PLAN_COMPILED

NODE_READY

WORKER_STARTED

WORKER_COMPLETED

GATE_PASSED

GATE_FAILED

RETRY_REQUESTED

CONTEXT_REBUILD_REQUIRED

WORKER_ESCALATION_REQUIRED

ARCHITECTURAL_DECISION_REQUIRED

NODE_COMPLETED

MILESTONE_COMPLETED

INTEGRATION_FAILED

PRODUCT_VERIFIED

RECIPE_CANDIDATE_CREATED

RECIPE_PROMOTED

DRIFT_DETECTED

Most events are handled deterministically.

Astra/high-level reasoning is invoked only on designated decision events.

13. Parallel Production

Parallelism is a DAG property, not an agent count.

Example:

                   PRODUCT SPEC
                       │
                 ARCHITECTURE
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       DATA/API       UI BASE     CONTENT
          │            │            │
          └──────┬─────┴─────┬──────┘
                 ▼           ▼
             INTEGRATION   ACCESSIBILITY
                 │           │
                 └─────┬─────┘
                       ▼
                   SYSTEM TEST

Parallel execution must preserve:

isolated workspaces;

explicit interface contracts;

deterministic merge/integration ordering;

conflict detection;

independent quality gates.

Wall-clock speed is measured separately from aggregate model compute.

14. Product Recipe Learning

Recipe learning is the central research mechanism.

Inputs

successful and failed run evidence;

product signatures;

station/task classes;

worker outcomes;

context manifests;

cost/latency;

gate outcomes;

retries/escalations;

graph revisions.

Output

A candidate recipe containing the best supported production configuration.

Promotion

V0 promotion is rule-based and human/audit approved.

Later versions may automate promotion, but always require:

evidence coverage;

quality non-regression;

reproducibility;

rollback path;

versioned audit trail.

No silent learning

Production behavior must never change invisibly because of accumulated traces.

Every promoted recipe is an explicit artifact.

15. Minimum Capable Worker Research

The system must not use vague statements such as "the model used only 50% of its intelligence."

Instead REDI Lab estimates operational sufficiency.

For each station/task class:

collect paired tasks;

evaluate multiple worker tiers;

compare verified success;

measure cost and latency;

estimate uncertainty;

identify the cheapest worker satisfying the predeclared quality criterion.

This yields an evidence-backed Minimum Capable Worker frontier.

Example conceptual result:

Station / Task Class

Small

Medium

Frontier

Selected

Unit-test addition

pass

pass

pass

Small

CRUD endpoint

unstable

pass

pass

Medium

Cross-service architecture

fail

unstable

pass

Frontier

No threshold is fixed permanently. Experiment acceptance margins must be preregistered after the pilot establishes realistic variance.

16. Metrics

Quality metrics

verified task success;

verified product success;

hidden-test success;

regression count;

scope violations;

integration failures;

security gate failures;

false-positive reviewer approvals;

human correction rate.

Efficiency metrics

total cost per verified product;

total tokens per verified product;

frontier-model token share;

frontier-model call share;

wall-clock time;

aggregate worker compute time;

retries per successful node;

cost per successful station;

context tokens per successful station.

Learning metrics

recipe reuse rate;

cold-start → stable-recipe runs required;

warm-start improvement;

MCW stability;

worker-selection regret;

recipe revision frequency;

drift detection delay;

successful downshift rate;

rollback rate after optimization.

Core composite views

Do not collapse research into one score initially.

At minimum report the Pareto frontier of:

quality;

cost;

wall-clock time;

frontier usage.

Later candidate metrics:

Verified Success per Euro;

Verified Success per 1M Context Tokens;

Frontier Avoidance at Fixed Quality;

Time-to-Stable-Recipe.

17. Experimental Baselines

Every claimed improvement must compare against relevant baselines.

B0 — Frontier Monolith

One strong model performs the whole task/product under a high-quality standard harness.

B1 — Fixed Multi-Agent

Static Planner → Builder → Tester/Reviewer workflow with fixed models.

B2 — Cost Router

Task-level model routing without product recipe memory.

B3 — Factory No-Learning

REDI Method contracts/gates but no recipe reuse.

B4 — Recipe-Based Factory

Recipe reuse enabled.

B5 — Recipe + Continual Optimization

Recipe + controlled exploration/downshifting.

Where feasible, evaluate against public software-engineering benchmarks and established harnesses rather than only self-created examples.

18. Experimental Program

Experiment Family A — Orchestration Correctness

Goal: prove the laboratory itself is deterministic/replayable enough for research.

Test:

state transitions;

retries;

crashes/resume;

isolated workspaces;

event replay;

evidence completeness;

cost/token accounting;

recipe versioning.

No intelligence claim is valid until this passes.

Experiment Family B — Role/Station Specialization

Question:

Does a bounded station contract let a weaker worker match a stronger generalist on that class of work?

Variables:

generic prompt vs Station Contract;

same model vs different model sizes;

context equalized.

Experiment Family C — Context Isolation

Question:

Can selected context outperform or match larger generic context?

Conditions:

full repo;

selected files;

selected files + architecture contracts;

selected files + station memory;

varying context budgets.

Experiment Family D — Quality Independence

Compare:

builder self-review;

same-model fresh reviewer;

different-model reviewer;

deterministic tests + reviewer;

multi-gate verification.

Measure false acceptance, cost, latency.

Experiment Family E — Unknown Product Discovery

Give REDI Lab a product class with no recipe.

Measure:

number of worker trials;

escalations;

graph revisions;

cost;

quality;

time to first verified product;

evidence required before first recipe candidate.

Experiment Family F — Recipe Reuse

Produce multiple unseen products in the same class.

Compare cold-start vs warm-start recipe.

Measure:

quality;

cost;

time;

frontier intervention;

required replanning;

recipe transfer failures.

Experiment Family G — Continual Downshifting

Introduce cheaper/smaller workers after a stable recipe exists.

Test whether REDI Lab can discover where they safely replace stronger workers without reducing product quality.

Experiment Family H — Model/Provider Drift

Change model version/provider/runtime.

Measure whether:

drift is detected;

recipe confidence is reduced appropriately;

revalidation targets affected stations rather than rebuilding everything;

rollback works.

Experiment Family I — Dynamic Topology

Compare:

fixed workflow;

product-specific DAG;

learned/reused DAG.

Test whether product-class topology materially affects quality/cost/time.

19. Test Product Strategy

Do not evaluate REDI Lab only on toy calculators.

Tier 1 — Synthetic controlled products

Purpose: laboratory validation.

Examples:

Todo REST API;

small inventory service;

simple React UI;

URL shortener;

notes application.

Use deterministic/hidden tests.

Tier 2 — Curated realistic products

Purpose: production-graph and recipe research.

Initial archetypes:

business website;

CRUD SaaS;

REST API;

CLI utility.

Each archetype should have multiple distinct orders, not copies of one specification.

Tier 3 — Existing repositories

Purpose: modification/maintenance tasks and context selection.

Use curated issues with known expected behavior.

Tier 4 — Public benchmarks

Purpose: external comparability.

Integrate established software-engineering benchmarks where licensing and infrastructure permit.

20. Threats to Validity

REDI Lab must explicitly defend against misleading results.

Evaluator correlation

Builder and reviewer from the same model family may share blind spots.

Mitigation:

deterministic tests;

hidden tests;

cross-model reviewer experiments.

Test leakage

Workers must not see hidden evaluation tests.

Benchmark contamination

Public benchmark success may reflect training exposure.

Therefore internal unseen product tests remain necessary.

Model drift

Provider aliases may change behavior.

Store exact model/version identifiers where available and date every run.

Pricing drift

Store price schedule/version used to calculate cost.

Environment drift

Pin dependencies and store environment manifests.

Cache effects

Separate cold/warm cache timing where relevant.

Parallelism distortion

Report wall-clock and aggregate compute independently.

Human intervention

Every manual correction must be logged; otherwise autonomy results are invalid.

Cherry-picking

Experiment suites and acceptance rules must be fixed before viewing final comparative results.

21. Security and Execution Boundaries

V0 defaults:

isolated workspace per worker;

no production credentials;

no destructive cloud operations;

network access denied unless explicitly required;

secrets never placed in prompts/traces;

path/scope allowlists;

tool permissions per Station Contract;

hard token/cost/time budgets;

audit log for every action.

Security gates become more strict, not less, as autonomy increases.

22. Data Policy

Three classes:

A. Public research data

Safe to publish:

synthetic tasks;

public benchmark manifests;

normalized metrics;

non-sensitive recipes;

public repo patches under compatible licenses.

B. Private research data

Not published:

private source code;

proprietary traces;

credentials;

customer information;

private repository contents.

C. Derived evidence

May be publishable only when it cannot reconstruct private source material.

Dataset/publication policy must be explicit before external release.

23. Core Data Contracts

The following objects are mandatory before implementation.

ProductSpec

Describes the order and acceptance boundaries.

ProductClass

Describes a reusable class signature and compatibility criteria.

ProductRecipe

Versioned production configuration and evidence confidence.

ProductDAG

Versioned graph of production work.

StationContract

Stable role definition.

WorkerProfile

Versioned candidate worker configuration.

TaskContract

Bounded unit of work.

ContextManifest

Exactly what context was supplied and why.

HandoffManifest

Structured output from one station to downstream consumers.

GateResult

Independent verification result and evidence.

RunEvidence

Normalized execution observation.

FailureRecord

Taxonomized failure.

ExperimentManifest

Reproducible experimental setup.

FactoryRun

Top-level production execution.

All contracts require schema versions.

24. Proposed Repository Structure

redi-lab/
│
├── README.md
├── LICENSE                  # decide before public release
├── pyproject.toml
│
├── docs/
│   ├── PROJECT_PLUTO_PLAN.md
│   ├── research-charter.md
│   ├── architecture.md
│   ├── experiment-policy.md
│   ├── data-policy.md
│   └── threat-model.md
│
├── redi_lab/
│   ├── domain/
│   │   ├── products/
│   │   ├── recipes/
│   │   ├── stations/
│   │   ├── workers/
│   │   ├── tasks/
│   │   ├── evidence/
│   │   └── experiments/
│   │
│   ├── orchestrator/
│   ├── dag/
│   ├── scheduler/
│   ├── events/
│   ├── context/
│   ├── gates/
│   ├── routing/
│   ├── optimizer/
│   ├── recipes/
│   ├── telemetry/
│   └── persistence/
│
├── adapters/
│   ├── models/
│   │   ├── openai/
│   │   └── future/
│   ├── runtime/
│   │   ├── openai_agents/
│   │   └── future/
│   ├── sandbox/
│   └── git/
│
├── stations/
│   ├── builder/
│   ├── tester/
│   └── reviewer/
│
├── experiments/
│   ├── manifests/
│   ├── fixtures/
│   └── reports/
│
├── benchmarks/
│   ├── synthetic/
│   ├── curated/
│   └── external/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── replay/
│   └── acceptance/
│
└── data/
    └── .gitkeep

This is a target structure, not a requirement to create every directory immediately.

25. Initial Technical Stack

Keep V0 intentionally small.

Core

Python;

typed domain models;

SQLite;

Git;

pytest.

First runtime adapter

OpenAI Agents SDK / current OpenAI agent runtime where useful.

Provider abstraction

REDI Lab owns:

ModelAdapter;

WorkerRunner;

SandboxAdapter;

TraceAdapter.

This prevents vendor lock-in.

Later, only when justified

PostgreSQL;

distributed queue;

Temporal/durable workflow engine;

local model runtime;

web dashboard;

remote workers.

Do not add these to V0 without a measured need.

26. Development Milestones

REDI-00 — Research Charter

Goal: lock the scientific question before code.

Deliverables:

this project plan;

formal research charter;

novelty/non-novelty boundary;

experiment policy;

metric definitions;

data policy;

threat model.

Acceptance:

hypotheses are falsifiable;

terms are unambiguous;

no product claim relies on unmeasured assumptions.

REDI-01 — Domain Contracts & Event Model

Goal: define the factory language.

Implement schemas for:

ProductSpec;

ProductClass;

ProductRecipe;

ProductDAG;

StationContract;

WorkerProfile;

TaskContract;

ContextManifest;

HandoffManifest;

GateResult;

RunEvidence;

FactoryRun;

events/failure taxonomy.

Acceptance:

schema versioning;

validation tests;

round-trip serialization;

no model/provider dependencies inside domain contracts.

REDI-02 — Evidence Ledger & Replay Core

Goal: make every experiment auditable before adding autonomy.

Implement:

SQLite persistence;

experiment manifests;

event ledger;

normalized run evidence;

replay/read-only reconstruction;

cost/time/token fields;

environment/version capture.

Acceptance:

a run can be reconstructed from stored state;

incomplete evidence causes test failure;

repeated replay does not mutate historical evidence.

REDI-03 — Minimal Factory Line

Goal: prove end-to-end event-driven production.

Flow:

ProductSpec → Task → Builder → deterministic gates → Reviewer → final result

Use one provider initially.

Acceptance:

isolated execution;

builder cannot self-approve;

deterministic event transitions;

retry/escalation captured;

crash/resume test;

full evidence record.

No recipe learning yet.

REDI-04 — Station & Worker Pools

Goal: separate role from model.

Add:

Builder/Test/Reviewer Station Contracts;

multiple eligible WorkerProfiles per station;

deterministic worker-selection policy;

fallback/escalation ladder.

Acceptance:

same station can switch workers without changing task/product contracts;

worker comparison is recorded as evidence;

no provider-specific fields leak into StationContract.

REDI-05 — Context Compiler / Firewall

Goal: make context a controlled experimental variable.

Implement:

context manifests;

allowlisted inputs;

context budgets;

relevant-file selection baseline;

upstream handoff inclusion;

context hash/replay.

Acceptance:

exact context can be reproduced;

unrelated station history is excluded by default;

full-context vs selected-context experiment is executable.

REDI-06 — Product DAG & Parallel Scheduler

Goal: move from linear workflow to production graph.

Implement:

dependencies;

ready-node computation;

isolated parallel execution;

downstream handoff contracts;

integration conflict handling.

Acceptance:

independent nodes run concurrently;

blocked nodes cannot start;

graph revisions are versioned;

wall-clock vs aggregate compute measured separately.

REDI-07 — Cold-Start Exploration

Goal: support a product with no known recipe.

Implement:

unknown-product mode;

conservative initial DAG;

strict gates;

candidate worker exploration;

failure-aware retry/escalation;

recipe-candidate generation.

Acceptance:

first verified product produces an evidence-backed recipe candidate;

failed attempts remain in evidence;

no recipe promotion from one unverified trajectory.

REDI-08 — Recipe Store & Warm Start

Goal: reuse production knowledge.

Implement:

product-class matching;

recipe versioning;

warm-start DAG/configuration;

recipe adaptation;

confidence metadata;

rollback.

Acceptance:

second product in same class can execute from recipe;

recipe provenance is traceable;

cold-start and warm-start can be A/B compared.

REDI-09 — Minimum Capable Worker Experiments

Goal: quantify where smaller workers are sufficient.

Implement:

paired worker trials;

station/task stratification;

confidence reporting;

cost/quality frontier reports.

Acceptance:

results cannot be reduced to model leaderboard claims;

worker sufficiency is tied to station/task class;

no "percentage intelligence" claims.

REDI-10 — Continual Optimization

Goal: safely improve a stable recipe.

Implement:

exploration budget;

candidate downshift experiments;

shadow runs;

promotion rules;

drift detection;

targeted revalidation.

Acceptance:

production recipe cannot change without recorded experiment evidence;

regression triggers rollback;

new model onboarding does not require architecture changes.

REDI-11 — External Benchmark Validation

Goal: establish comparability outside REDI Lab-created tasks.

Add:

selected public SWE benchmarks/harnesses;

reproducible adapters;

baseline comparisons.

Acceptance:

report exact benchmark version;

preserve benchmark isolation;

publish negative results as well as positive ones.

REDI-12 — Research Release / Product Decision

Goal: decide whether REDI Lab is research, open-source, commercial, or hybrid.

Required before decision:

repeated experimental results;

clear advantage/disadvantage map;

cost model;

security review;

legal/license review;

publication/IP decision.

Potential directions:

open research spec + commercial control plane;

developer platform;

self-hosted enterprise factory;

recipe/benchmark infrastructure;

worker/model optimization service.

Do not choose the business model before evidence establishes what REDI Lab actually does better.

27. Initial V0 Scope

V0 is deliberately narrow.

V0 includes

one product at a time;

one repository;

three station types: Builder, Tester, Reviewer;

one strong orchestrator;

initially one provider with at least two worker configurations;

SQLite evidence store;

event-driven execution;

deterministic gates;

structured handoffs;

experiment/replay manifests;

no UI.

V0 explicitly excludes

learned routing;

autonomous recipe promotion;

fine-tuning;

local model hosting;

distributed execution;

customer accounts;

billing;

SaaS;

Kubernetes;

vector database unless an experiment proves it necessary;

10+ agent roles.

28. First Research Sequence

The first useful experiments should be performed in this order:

R1 — Harness validity

Can REDI Lab execute, verify, record, and replay correctly?

R2 — Independent verification

Does separation of Builder and Reviewer reduce false success?

R3 — Context experiment

Selected context vs full repo on identical tasks.

R4 — Worker downshift

Strong worker vs cheaper worker on bounded station tasks.

R5 — Cold-start product

Produce an unseen small app; collect full evidence.

R6 — Recipe induction

Create first product-class recipe.

R7 — Warm-start sibling product

Produce a different app from the same class.

R8 — Recipe advantage

Compare warm-start against cold-start baseline.

Only after R1–R8 should REDI Lab implement a learned optimizer.

29. First Product-Class Study

Recommended first serious archetype:

business_web_v1

Why:

understandable acceptance criteria;

enough separable stations;

frontend/content/data/contact forms/SEO/accessibility/integration;

parallelizable work;

can generate multiple unseen specifications;

hidden tests and browser checks are possible;

close enough to real commercial software without initially requiring high-risk financial/authentication logic.

Required sibling products must differ materially in:

industry;

content structure;

feature set;

visual requirements;

forms/integrations;

accessibility/SEO constraints.

The experiment must prevent REDI Lab from simply copying one prior site.

The second archetype should be structurally different, e.g. crud_saas_v1, to test whether recipes remain product-class-specific rather than becoming one universal workflow.

30. Research Decision Gates

Gate A — Laboratory Trustworthy?

Do not interpret model results until:

evidence is complete;

replay works;

hidden tests remain hidden;

environment is reproducible;

cost/token data is trustworthy.

Gate B — Specialization Has Value?

If role contracts/context isolation do not improve any meaningful quality/cost frontier, stop investing in specialization and revise the thesis.

Gate C — Recipe Transfer Exists?

If recipes do not improve unseen sibling products, REDI Lab must not claim production learning.

Gate D — Downshifting Works?

If smaller workers create excessive verification/retry cost, do not optimize model size merely because inference price is lower.

Gate E — Product-Level Advantage?

The final comparison is product quality/cost/time, not per-call price.

31. Publication / IP Discipline

Until the research advantage is understood:

keep architecture and experiments in a private repository;

preserve dated commits and experiment manifests;

avoid public claims of novelty before comparative research;

do not publish private customer/project code as datasets;

document independent design decisions and prior art;

if a genuinely novel mechanism with commercial value emerges, obtain qualified IP/legal advice before public disclosure.

Do not assume that a broad "AI software factory" concept is protectable or unique.

32. Potential Long-Term Asset

If REDI Lab succeeds, the defensible asset is unlikely to be "we have many agents."

It is more likely to be the accumulated combination of:

station/task taxonomy;

verified production evidence;

product-class recipe library;

context-selection strategies;

worker capability profiles;

routing/downshift policies;

drift/revalidation data;

cross-model/product performance history;

reproducible benchmark methodology.

This dataset and process knowledge can later inform:

better routing;

specialized small workers;

fine-tuning/distillation;

local models;

a future specialized worker model.

33. Success Definition

REDI Lab succeeds as research only if it can demonstrate, on unseen tasks/products and against explicit baselines, at least one repeatable improvement in the production Pareto frontier:

equal verified quality at lower cost;

equal verified quality with lower frontier-model usage;

equal verified quality with less context;

equal verified quality in less wall-clock time;

higher verified quality at comparable cost;

faster adaptation for repeated product classes.

A visually impressive demo is not success.

A large number of agents is not success.

Low API cost with poor verification is not success.

The evidence must show that the factory learned a better way to produce software.

34. Immediate Next Action

Do not begin by implementing workers.

The next repo task is:

REDI-00A — Freeze the Research Charter

Create and review:

docs/research-charter.md

docs/experiment-policy.md

docs/data-policy.md

docs/threat-model.md

docs/architecture.md

Then implement REDI-01 domain contracts.

This keeps REDI Lab scientific, provider-independent, and auditable from its first commit.