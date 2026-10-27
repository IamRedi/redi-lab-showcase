# REDI Lab Research Charter

Document version: 1.1
Date: 2026-09-15
Milestone: REDI-00B
Status: Frozen REDI-00 governance baseline

---

## 1. Research Problem

Current AI-assisted software production systems suffer from one or more of the following structural limitations:

1. **Monolithic frontier dependence**: Whole-product execution relies on a single large generalist model, with no mechanism to determine which parts of the work actually require frontier capability.
2. **Fixed workflow rigidity**: Predefined agent chains (e.g., Planner → Coder → Reviewer) apply uniformly regardless of product class, ignoring structural differences in production requirements.
3. **Conversation-driven state**: System state is implicit in shared conversational history rather than explicit structured artifacts, making reproducibility, auditing, and transfer across products impossible.
4. **Model reputation over evidence**: Worker selection relies on global model rankings and pricing tiers rather than station-specific verified performance on comparable tasks.
5. **No product-class memory**: Successful production of a product in a given class does not reduce cost or improve speed for the next product in the same class; each run is effectively a cold start.
6. **Self-verification bias**: The same execution trajectory that produced an output is relied upon to judge its correctness, inflating apparent success rates.
7. **Unprincipled optimization**: Cost-reduction attempts (smaller models, shorter context, routing) are based on heuristics or isolated prompt tests, not controlled comparative evidence tied to end-to-end verified product quality.

These limitations mean that existing systems cannot answer the question: **What is the minimum production organization — workflow graph, station roles, worker assignment, context allocation, verification policy, escalation policy, and parallelism — required to produce a given class of software at a verified quality level?**

REDI Lab exists to answer this question experimentally.

---

## 2. Primary Thesis

A product-class-aware software factory can achieve equal or better verified product quality with lower frontier-model usage, lower context usage, lower cost, and/or lower wall-clock time than a monolithic frontier agent or a fixed multi-agent workflow, by learning and reusing evidence-backed production recipes from verified execution evidence.

This thesis is a conjunctive claim. It requires demonstration that the factory both:
- learns a production configuration from evidence, AND
- that configuration outperforms (on the quality/cost/time/frontier-usage Pareto frontier) the relevant baselines on unseen products.

---

## 3. Falsifiable Hypotheses

Each hypothesis below specifies the measurable outcome and the condition under which it is falsified. No hypothesis is a general claim about "intelligence" or "capability"; all are operational claims about REDI Lab factory behavior under specified experimental conditions.

### H1 — Station Specialization

**Claim**: For a bounded station task with an explicit contract, a weaker/cheaper worker operating under that contract achieves a verified success rate that is statistically non-inferior to a stronger generalist worker performing the same task with a generic prompt, when context budgets and tool access are equalized.

**Measurement**: Per-station verified success rate (deterministic gates + independent model review), paired comparison across matched task instances.

**Falsification**: Across a preregistered set of station/task classes, no station shows non-inferiority of the specialized weaker worker at the preregistered margin.

### H2 — Context Isolation

**Claim**: Role-specific context, compiled from the task contract, station contract, upstream handoff manifests, and a selected subset of source material, achieves a verified success rate that is statistically non-inferior to full repository context, while using fewer input tokens.

**Measurement**: Per-node verified success rate, input token count, cross-station contamination rate (measured by scope violations or irrelevant references in output).

**Falsification**: Across preregistered task classes, selected-context success is inferior to full-context success at the preregistered margin, or token savings fall below a preregistered negligible-savings threshold. The negligible-savings threshold is established by pilot calibration (ExperimentPolicy §18) and preregistered in the ExperimentManifest before the formal experiment.

### H3 — Minimum Capable Worker (MCW)

**Claim**: For many recurring station/task classes, there exists a worker below the current global frontier model whose verified performance on that bounded class is statistically non-inferior to the frontier worker's performance on the same class.

**Measurement**: Paired station/task experiments across at least three worker tiers (small/medium/frontier or equivalent), per-class non-inferiority tests.

**Falsification**: The fraction of tested station/task classes that admit a non-frontier MCW at the preregistered quality margin falls below a preregistered minimum-fraction threshold. The minimum-fraction threshold is established by pilot calibration (ExperimentPolicy §18) and preregistered in the ExperimentManifest before the formal experiment.

### H4 — Independent Quality Gates

**Claim**: Separating production from independent verification (different context, optionally different worker family) reduces the false-positive rate of apparent task success compared with self-verification by the producing worker.

**Measurement**: False-acceptance rate against hidden tests and post-hoc manual audit, comparing self-verified vs independently verified production runs.

**Falsification**: Independent verification does not reduce false-acceptance rate by the preregistered minimum margin, or increases cost/latency beyond preregistered bounds without a compensating quality improvement.

### H5 — Product Recipe Learning

**Claim**: Verified executions across multiple products in the same class can be aggregated into a reusable Product Recipe that specifies DAG topology, station selection, context policies, worker preferences, verification policies, and escalation policies.

**Measurement**: Recipe existence after N runs, recipe completeness (all required fields populated with evidence-backed values), reproducibility of recipe-specified configuration.

**Falsification**: After a preregistered number of successful products in a class, no recipe can be produced that compiles and executes without manual intervention on a sibling product.

### H6 — Warm-Start Production

**Claim**: A validated recipe, when used to warm-start production of an unseen sibling product in the same class, reaches final verified quality with:
- lower total cost;
- lower frontier-model token share;
- fewer architectural escalations;
- fewer graph revisions;
than cold-start (recipe-free) production of the same product.

**Measurement**: Matched sibling-product pairs; per-metric paired comparison; preregistered minimum improvement margin.

**Falsification**: Warm-start fails to improve any primary metric by the preregistered margin, or regresses verified quality.

### H7 — Continuous Downshifting

**Claim**: Given a stable validated recipe for a product class, the continual optimizer can, through controlled shadow or exploration runs, identify at least one station where a smaller/cheaper worker or a smaller context budget can replace the current worker without verified quality regression, and this substitution persists over a preregistered revalidation window.

**Measurement**: Successful downshift adoption rate, post-promotion revalidation pass rate, quality regression rollback rate.

**Falsification**: The fraction of proposed downshift candidates that survive promotion and revalidation falls below a preregistered minimum-survival threshold, or aggregate cost improvement is below the preregistered threshold. Both thresholds are established by pilot calibration (ExperimentPolicy §18) and preregistered in the ExperimentManifest before the formal experiment.

### H8 — Adaptive Factory Topology

**Claim**: Different product classes benefit from materially different production DAG structures (node count, station composition, dependency depth, parallelism degree, verification depth). A product-class-specific DAG, derived from or selected by evidence, achieves a better quality-cost Pareto point than a single fixed workflow applied uniformly across classes.

**Measurement**: Cross-class comparison of fixed-workflow vs class-optimized-DAG outcomes on preregistered product archetypes.

**Falsification**: No product class shows a preregistered Pareto improvement from topology adaptation.

### H9 — Production Evidence Beats Model Reputation

**Claim**: Within REDI Lab's worker selection for a specific station/task class, a policy that uses historical station-specific verified evidence (success rate, failure taxonomy, cost, latency) outperforms a policy that uses only global model reputation signals (provider model tier, public leaderboard rank, price per token) on the metric of verified success per unit cost.

**Measurement**: A/B comparison of evidence-based selection vs reputation-based selection on held-out task instances.

**Falsification**: Evidence-based selection is not superior by the preregistered margin.

---

## 4. Terminology

This section is normative. All REDI Lab documents and code must use these terms consistently.

| Term | Definition |
|---|---|
| **Product Order** | An external request to produce a software artifact. Contains requirements, constraints, acceptance criteria, budget, and risk level. |
| **ProductSpec** | The normalized internal representation of a product order, with explicit uncertainty markers for unknown requirements. |
| **Product Class** | A reusable family of products with sufficiently similar production structure to justify transfer of a Product Recipe. Membership is determined by production-structure similarity, not code similarity. |
| **Station** | A stable production responsibility defined by a StationContract. A station is a role, not a model. Stations are provider-independent. |
| **StationContract** | A normalized, versioned specification of a station's responsibility, accepted inputs, required outputs, allowed tools, owned scope, forbidden scope, required quality gates, compatible task classes, candidate worker pool, and handoff schema. |
| **Worker** | A specific model/runtime configuration capable of operating one or more stations. A worker identity includes: provider, model/version, reasoning level, tool permissions, context limit/budget, execution policy, and cost profile. |
| **WorkerProfile** | A versioned record of a worker's identity, eligibility, historical evidence, reliability, availability, and cost/latency observations. |
| **Worker Pool** | The set of candidate workers eligible for assignment to a station. |
| **Minimum Capable Worker (MCW)** | For a specific station/task class, the least expensive/least capable tested worker that meets the predefined quality criterion with sufficient evidence. An operational, measured concept, not an intelligence ranking. |
| **TaskContract** | A bounded, versioned unit of work assigned to a station. Contains station type, prerequisites, outputs, downstream consumers, verification requirements, risk level, parallelizability, and escalation policy. |
| **Product DAG** | A versioned dependency graph whose nodes are TaskContracts and whose edges are prerequisite relationships. The structure of production. Derived parallelism is a property of this graph. |
| **ContextManifest** | An exact, reproducible, hashed specification of the context package supplied to a worker: source files (with versions/hashes), handoff manifests, contracts, summaries, prior failure evidence, and the selection rationale. |
| **Context Firewall** | The component that compiles a ContextManifest from available information and enforces that no out-of-scope material enters the worker's context. |
| **HandoffManifest** | The structured output artifact from a completed station node, presented to downstream consumers. Must conform to the StationContract's handoff schema. |
| **Quality Gate** | An independent decision boundary. Deterministic gates are software-enforced (compilation, tests, lint, schema, scope, secrets). Model-based gates are independent reviewer judgment on criteria the producing worker did not use for self-evaluation. |
| **GateResult** | A versioned record of a gate evaluation: gate type, criteria, inputs, result, evidence, and reviewer identity (if model-based). |
| **Factory Configurator / Orchestrator** | The high-level intelligence component. Determines recipe reuse vs exploration, compiles the initial Product DAG, sets gate strictness and exploration budgets, and handles architectural escalations. Maintains management state; does not accumulate full implementation history. |
| **Evidence Ledger** | The immutable append-only research backbone. Every attempt (success or failure) records a RunEvidence entry with exhaustive execution provenance. |
| **RunEvidence** | Normalized structured observation from a single node attempt: identities (run, experiment, product, recipe, DAG, node, station, worker), context manifest hash, token counts, timing, files read/changed, tests, gate results, retries, escalations, human interventions, cost, failure taxonomy, source revision, and environment manifest. |
| **Product Recipe** | A versioned, evidence-backed production configuration for a Product Class. Contains: product-class signature, required stations, DAG template, task decomposition patterns, worker preferences and fallback ladders, context strategies and budgets, verification policies, retry policies, escalation policies, parallelization rules, expected distributions, confidence/evidence coverage, validity conditions, and last revalidation state. |
| **Recipe Store** | The versioned persistence layer for Product Recipes. Promotion and rollback are explicit, auditable operations. |
| **Continual Optimizer** | The component that proposes candidate optimizations (worker downshift, context reduction, topology change, parameter adjustment) and evaluates them under controlled experiments before recommending recipe promotion. Proposes only; does not silently mutate recipes. |
| **Exploration Mode** | Operating mode for products with no compatible validated recipe. Characterized by conservative DAG, stricter gates, broader worker exploration, earlier escalation, and evidence retention. |
| **Production Mode** | Operating mode for products with a compatible validated recipe. Characterized by recipe warm-start, evidence-preferred workers, normal gates, bounded exploration fraction, and outcome-vs-expectation monitoring. |
| **Drift Mode / Revalidation** | Operating mode triggered by model version change, provider behavior change, repeated degradation, cost/latency shift, tool/runtime change, requirement change, or new candidate model. Freezes promotion and runs targeted revalidation before promote/retain/rollback. |
| **Escalation** | A policy-triggered event: RETRY (same worker, bounded), CONTEXT_REBUILD (recompile context, retry), WORKER_ESCALATION (stronger worker), or ARCHITECTURAL_DECISION (orchestrator judgment). |
| **Failure Taxonomy** | The exhaustive classification of node failures: spec ambiguity, context insufficiency, context contamination, capability insufficiency, tool failure, environment failure, dependency mismatch, scope violation, deterministic test failure, reviewer rejection, integration conflict, security violation, timeout/budget exhaustion, orchestrator planning error. |
| **ExperimentManifest** | A preregistered, versioned specification of an experiment: hypothesis, variables, baselines, task set, selection criteria, sample size, randomization method, repetition count, stopping rule, statistical test, acceptance threshold, and analysis plan. |
| **Baseline** | A control system against which REDI Lab configurations are compared. Defined for each experiment family. |
| **Pareto Frontier** | The set of production configurations such that no configuration can improve one metric (quality, cost, wall-clock, frontier usage) without degrading another. REDI Lab reports movement on this frontier, not a single scalar score. |
| **REDI Method** | The combined set of domain concepts, contracts, evidence policies, optimization rules, and operating modes that define REDI Lab's research object. Provider-independent. |

---

## 5. Research Questions

These questions structure the experimental program. Answering them requires comparative evidence, not single demonstrations.

1. **Q1 — Orchestration Correctness**: Can the REDI Lab laboratory execute, record, verify, and replay production runs deterministically enough that experiment results are attributable to treatment differences rather than harness noise?
2. **Q2 — Role Specialization**: Do bounded station contracts enable weaker workers to match stronger generalists on matched tasks with equalized context and tools?
3. **Q3 — Context Allocation**: What context-selection strategies (full repo, selected files, contracts + handoffs, summaries, prior-failure inclusion) yield the best verified success per input token, and how does this vary by station/task class?
4. **Q4 — Independence of Verification**: How much does independent verification (separate worker, separate context) reduce false acceptance rates, and at what cost/latency?
5. **Q5 — Recipe Learnability**: How many verified successful productions of a class are required before a reusable recipe can be compiled, and what is the minimum evidence coverage for recipe validity?
6. **Q6 — Recipe Transfer**: On unseen sibling products in the same class, what is the magnitude of improvement (cost, frontier usage, time, escalations) from warm-starting from a validated recipe versus cold-starting?
7. **Q7 — MCW Frontier**: What fraction of station/task classes admit a non-frontier MCW? How stable is the MCW assignment over time and across task variants?
8. **Q8 — Topology Adaptation**: How much does product-class-specific DAG topology (nodes, dependencies, parallelism, verification depth) improve the Pareto frontier over a single fixed workflow?
9. **Q9 — Continuous Optimization**: Can the optimizer safely discover and adopt worker/context/topology improvements in stable recipes without quality regression, and at what rate?
10. **Q10 — Drift Resilience**: How quickly and accurately does the system detect model/provider/environment drift, and how targeted is the resulting revalidation?
11. **Q11 — Evidence vs Reputation**: Under what conditions does station-specific historical evidence outperform global model reputation as a worker-selection signal?
12. **Q12 — Product-Level Advantage**: At the full-product level, what is the shape and magnitude of the REDI Lab Pareto frontier versus the frontier-monolith and fixed-multiagent baselines across preregistered product classes?

---

## 6. Novelty Boundary

This section is normative. REDI Lab publications, internal reports, code comments, and external communications must not claim the following as REDI Lab inventions, either explicitly or through omission of prior-art attribution.

**Governance Review Disclaimer**: The prior-art list in Section 6.1 is illustrative, not an exhaustive prior-art search. Other relevant prior art may exist. This section is an internal governance boundary for REDI Lab communications, not a legal patentability determination or an external prior-art search report. REDI Lab does not claim to have independently verified the completeness or legal status of the prior-art items listed.

### 6.1 Established Prior Art — Not Claimed

REDI Lab explicitly does **not** claim invention or origin of the following concepts and systems, which exist publicly and are used by REDI Lab as background and building blocks:

- Multi-agent systems and role-based agent specialization
- Planner / Coder / Reviewer chains and fixed multi-agent workflows
- Software-factory and assembly-line metaphors for software production
- Model routing based on price, capability, or task characteristics
- Quality gates and independent reviewer/validator roles
- Agent tracing, telemetry, and execution logging
- Sandbox execution, workspace isolation, and tool-permission scoping
- Workflow memory and reusable planning artifacts
- Dynamic role assignment and task allocation
- Long-running agent harnesses and durable execution
- Context window management, retrieval-augmented generation, and summarization
- Bandit-based and reinforcement-learning-based selection policies
- A/B testing of model configurations

### 6.2 REDI Lab Research Claim — the Combined Learning Problem

REDI Lab's **hypothesized research direction** (not a proven contribution), pending experimental evidence from the Evidence Ledger, is the combined production-level learning problem, specifically:

**The end-to-end discovery, storage, reuse, validation, and continuous re-optimization of the complete production configuration for a product class — Production DAG → Stations → Worker Pools → Context Policies → Quality Gates → Retry/Escalation Policies → Parallelism → Product Recipe — where every configuration choice is tied to, revised by, and reversible on the basis of normalized evidence from independently verified production runs.**

This hypothesized claim is differentiated from prior art in the following specific ways, which themselves require experimental support before they may be asserted as contributions:
1. The unit of learning is the **full production configuration for a product class**, not a single task routing decision or a prompt template.
2. Transfer is explicit through **versioned Product Recipes with evidence-coverage metadata**, not implicit workflow memory.
3. Verification and optimization are **integrated into the same evidence loop**: gate results determine recipe candidates, and recipe candidates are revalidated against gates.
4. Worker selection uses **station-class-specific historical evidence** rather than global reputation or single-call routing features.
5. Optimization candidates are **proposed, experimented, and promoted through an explicit controlled pipeline**, never through silent accumulation of traces.
6. The entire configuration, including context packages, is **reproducible, hashed, and replayable**.

### 6.3 Claim Discipline

No REDI Lab communication (internal or external) may assert any of the following without citing specific comparative experimental evidence from the Evidence Ledger:
- That REDI Lab is "better" than any baseline system at any metric
- That any particular hypothesis (H1–H9) is true
- That a Product Recipe is valid for a class
- That a worker is a Minimum Capable Worker for a station/task class
- That any optimization proposal is safe to promote

Marketing language, qualitative claims of "intelligence," "understanding," or "autonomy" without quantified evidence, and claims of unique architectural novelty without prior-art citation are explicitly forbidden.

---

## 7. Non-Goals

REDI Lab research explicitly does not target the following in REDI-00 through REDI-12:

1. **Natural-language chat-based end-user interface**: Research communications and results are structured artifacts. No conversational UX claims.
2. **General-purpose AI assistant or coding copilot**: The factory produces verified software products. It is not an autocomplete, inline suggestion, or pair-programming chatbot.
3. **Model training / fine-tuning / distillation**: V0 through REDI-12 use off-the-shelf models behind Worker adapters. Evidence from REDI Lab may inform future training projects, but training is not part of the current research scope.
4. **Local model hosting and inference optimization**: Worker runtime is delegated to adapters. Local hosting may be added later as a worker option, but the research target is the factory, not inference.
5. **SaaS platform, user authentication, billing, multi-tenancy**: Commercial infrastructure is deferred until REDI-12's product/research decision gate.
6. **Vector databases, long-term "agent memory," or embeddings-based retrieval as ends in themselves**: These may enter as candidate context-selection strategies within the Context Compiler, but only as controlled experimental variables, not as architectural requirements.
7. **Distributed execution, cluster orchestration, or cloud-scale infrastructure**: V0 through REDI-12 use single-process or simple parallel execution. Temporal, queues, Kubernetes, etc., are deferred.
8. **IP, licensing, or business-model claims as research output**: The REDI-12 gate addresses these as separate decisions, contingent on research results.
9. **A universal "best workflow" for all software**: The research explicitly hypothesizes (H8) that different product classes require different topologies. Finding a single universal workflow is not a goal.
10. **Claims about model psychology, reasoning, or sentience**: All claims are operational and measured.

---

## 8. Success Criteria

REDI Lab succeeds as research if and only if **all** of the following conditions are met after the REDI-00 through REDI-11 experimental program:

### S1 — Laboratory Validity (Gate A)
- The evidence ledger is complete and replayable for every recorded run.
- Hidden tests remain hidden with zero leakage across the recorded experimental corpus.
- Cost, token, and timing accounting is internally consistent and auditable.
- Environment and version capture is sufficient for replay within documented tolerance.

### S2 — Non-Trivial Specialization (Gate B)
- At least two of H1 (station specialization), H2 (context isolation), or H4 (independent gates) are not falsified at their preregistered margins in at least one product class.
- The effect magnitude is large enough to be operationally meaningful (preregistered minimum improvement).

### S3 — Recipe Transfer (Gate C)
- H5 and H6 are not falsified: validated recipes exist for at least two product classes (this minimum is a current governance-design floor for generality, not an empirically validated scientific threshold) and demonstrate warm-start improvement over cold-start on unseen sibling products at the preregistered margin.

### S4 — Downshifting Potential (Gate D)
- H3 and H7 are not falsified: the fraction of tested station/task classes admitting a non-frontier MCW meets or exceeds the preregistered minimum-fraction threshold, and the fraction of proposed downshift candidates surviving promotion and revalidation meets or exceeds the preregistered minimum-survival threshold. Both thresholds are established by pilot calibration (ExperimentPolicy §18) and preregistered before formal evaluation.

### S5 — Product-Level Pareto Movement (Gate E)
- For at least two preregistered product classes (this minimum is a current governance-design floor, not an empirically validated scientific threshold), the REDI Lab configuration (B5: Recipe + Continual Optimization) demonstrates a preregistered Pareto improvement over at least the frontier-monolith baseline (B0) and the fixed-multiagent baseline (B1) on verified unseen products.

### S6 — Reproducibility
- The top 3 most consequential positive experimental results (this count is a provisional engineering audit floor, not a validated statistical sample) are independently reproducible by a separate operator using only the experiment manifest and stored evidence.

### S7 — Negative Results Retained
- All preregistered experiments, including falsified hypotheses and non-improving variants, are retained in the evidence ledger with analysis, and are cited in any summary document reporting positive results.

---

## 9. Failure Criteria

REDI Lab fails as a research program — requiring thesis revision, major architectural redesign, or program termination — if **any** of the following conditions are met:

### F1 — Laboratory Unsoundness
- Hidden-test leakage is detected in a manner that could materially inflate measured success rates, and the root cause cannot be remediated without discarding a majority of accumulated evidence.
- Replay of recorded runs produces materially different outcomes due to non-determinism or incomplete manifest capture, and this cannot be fixed in the harness without redesigning the core evidence model.
- Evidence is found to have been altered after recording, violating the append-only ledger property.

### F2 — Core Thesis Falsification
- H6 (warm-start) is falsified across all tested product classes: validated recipes do not improve any primary metric over cold-start, or consistently regress quality.
- H8 (adaptive topology) is falsified across all tested classes: no product class benefits from topology adaptation over a fixed workflow.

### F3 — No Meaningful Frontier
- After REDI-11, across all tested product classes and all baselines (B0–B3), no REDI Lab configuration achieves a Pareto improvement on unseen products that exceeds measurement noise and is robust to preregistered sensitivity analyses.

### F4 — Cost of Verification Dominates
- The cost of independent quality gates, retries, and escalations systematically exceeds any cost savings from worker downshifting or context reduction, making the factory net more expensive than simpler baselines at equivalent quality.

### F5 — Insurmountable Drift
- Worker and recipe performance drifts so rapidly that revalidation cost exceeds the value of any learned optimization, meaning recipes are effectively obsolete before promotion.

---

## 10. Baseline Systems

Every experiment claiming an improvement must compare against at least the relevant subset of the following baselines. Which baselines are required is specified in the ExperimentManifest for each experiment family.

### B0 — Frontier Monolith
- **Configuration**: A single frontier-class model performs the entire product task end-to-end under a high-quality standard harness (same sandbox, same tool permissions, same total token/cost budget cap, same deterministic quality gates applied externally).
- **Purpose**: The strongest realistic single-model competitor. Tests whether factory structure adds value over simply throwing the best available model at the problem.
- **Required for**: All product-level comparisons (H6, H8, S5).

### B1 — Fixed Multi-Agent
- **Configuration**: A static linear or DAG workflow with fixed station roles (e.g., Planner → Builder → Tester → Reviewer), fixed model assignments (one per role), no worker pools, no recipe memory. Uses the same station contracts and deterministic gates as REDI Lab where applicable.
- **Purpose**: Tests whether the factory's adaptive/configurable elements (worker pools, recipe learning, context selection) add value beyond a well-structured but static multi-agent system.
- **Required for**: Product-level comparisons (H6, H8, S5); gate-specialization comparisons (H1) if the fixed multi-agent uses generalist prompts.

### B2 — Cost Router
- **Configuration**: Task-level model routing based on static rules, heuristics, or bandit algorithms using task features and global model reputation signals. No product-class recipe memory, no DAG-level learning, no transfer across products in a class.
- **Purpose**: Tests whether REDI Lab's evidence-backed recipe-level learning outperforms task-level routing without class memory.
- **Required for**: H9 (evidence vs reputation); worker-selection ablation studies.

### B3 — Factory No-Learning
- **Configuration**: Identical REDI Method contracts, gates, worker pools, and context policies as REDI Lab, but with recipe reuse **disabled**: every production run is a cold start; no recipe is consulted or updated between runs.
- **Purpose**: Isolates the value of recipe learning and transfer (H5, H6) by controlling for all other factory structure.
- **Required for**: H5, H6, any comparison where recipe transfer is the treatment.

### B4 — Recipe-Based Factory (No Continual Optimization)
- **Configuration**: B3 + recipe reuse enabled. Recipes are promoted after initial runs and then held fixed. No downshift experiments, no shadow runs, no optimizer-proposed changes between recipe versions.
- **Purpose**: Isolates the value of continual optimization (H7) and drift revalidation.
- **Required for**: H7, H10 (drift resilience).

### B5 — Recipe + Continual Optimization
- **Configuration**: Full REDI Lab: recipe reuse + controlled exploration/downshifting + drift detection and revalidation.
- **Purpose**: The full system. Used for final product-level Pareto comparisons.
- **Note**: B5 is the treatment, not a control. Experiments comparing configurations must hold other variables constant when comparing to B5.

### External Benchmark Baselines
- Where feasible, REDI Lab results on public software-engineering benchmarks must be compared against published results for other systems on the identical benchmark version, with explicit notes on any harness or setup differences.

---

## 11. Allowed and Disallowed Claims

This section is binding on all REDI Lab communications, including internal progress reports, external publications, code comments, and marketing collateral (should marketing exist before REDI-12).

### 11.1 Claims Allowed Without Evidence (Descriptive Claims)
- Claims about REDI Lab's **architecture, components, contracts, and intended research goals** as documented in this charter and the architecture document.
- Statements about the **structure of experiments** that have been preregistered but not yet run.
- Statements of the form "REDI Lab hypothesizes X" or "REDI Lab is investigating whether Y" when followed by a reference to the relevant hypothesis.
- Descriptions of prior art and the novelty boundary (Section 6), provided attribution is accurate.

### 11.2 Claims Allowed Only With Cited Evidence
- Any claim that REDI Lab, a configuration, a hypothesis, a recipe, a worker, or an optimization candidate **succeeds, improves, outperforms, is sufficient for, or is non-inferior to** a comparator on any metric.
- Any claim that a hypothesis (H1–H9) is "confirmed," "supported," "not falsified," or "verified."
- Any claim that a recipe is **validated, stable, promoted, or safe for production mode.**
- Any claim that a worker is the **Minimum Capable Worker** for a station/task class.
- Any claim about **cost savings, speedups, quality improvements, frontier-usage reduction,** or any other quantitative or comparative outcome.
- Any claim about the **shape or magnitude of the Pareto frontier.**

All such claims must:
1. Cite the specific ExperimentManifest(s) and run IDs in the Evidence Ledger.
2. State the baselines compared against.
3. State the preregistered acceptance threshold and whether it was met.
4. Report negative or non-significant results in the same document.

### 11.3 Claims Disallowed Under Any Circumstance Before REDI-12
- General claims of the form "REDI Lab builds better software than humans" or "REDI Lab is a superhuman software engineer."
- Claims of sentience, reasoning, understanding, agency, or psychological states in workers or the orchestrator.
- Claims that REDI Lab's architecture or method is "novel" or "invented" beyond the specific combined learning problem defined in Section 6.2.
- Claims that a particular model provider or model family is "best" or "worse" in general (as distinct from measured station-specific non-inferiority within REDI Lab's experiments).
- Claims about REDI Lab's commercial viability, market fit, or revenue potential.
- Claims about the security or safety of REDI Lab-generated code in production environments without independent external security audits.
- Any claim that relies on unpublished, unretained, or cherry-picked evidence.

---

## 12. Change Control

This Research Charter is frozen at REDI-00B as a governance baseline.

Cross-document review (REDI_LAB_RESEARCH_PLAN.md, research-charter.md, experiment-policy.md, data-policy.md, threat-model.md, architecture.md) was conducted at REDI-00B. **No unsupported factual claims were detected during the internal cross-document review.** This review does not constitute independent external verification of any prior-art citations or third-party claims.

Substantive changes require:
1. A documented rationale citing specific evidence or an identified logical flaw.
2. Review against the novelty boundary to ensure no redefinition of claims.
3. A version increment and dated changelog entry.
4. Recording in the evidence ledger under a charter-change administrative record.

Hypotheses (H1–H9) and the primary thesis may not be changed retroactively to accommodate null results; a falsified hypothesis remains falsified, and a new or revised hypothesis must be registered as a new experimental program.

---

## Governance Baseline

- **Milestone**: REDI-00B
- **Baseline commit SHA**: 97d07eae4d1cee6f798f2febff9fcb71bd42d04c
- **Date**: 2026-09-15

Future REDI Lab research policy changes must be versioned, reviewed, and auditable through subsequent commits. No substantive change to the research charter, experiment policy, data policy, threat model, or architecture document may enter force without a corresponding versioned commit with an explicit changelog and rationale.
