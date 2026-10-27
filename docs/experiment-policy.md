# REDI Lab Experiment Policy

Document version: 1.0
Date: 2026-09-15
Milestone: REDI-00A
Status: Frozen pending REDI-00A validation

---

## 1. Purpose and Scope

This document defines the rules under which a REDI Lab experiment is considered **valid research evidence**. No experiment result, whether positive or negative, may be cited in support of any research charter claim, used for recipe promotion, or incorporated into the Continual Optimizer's decision pipeline unless it conforms to this policy.

This policy applies to:
- All Experiment Families A through I defined in the Research Plan.
- All pilot experiments conducted to establish thresholds, variance estimates, or statistical parameters.
- All shadow runs, exploration runs, and revalidation runs that may influence production recipe selection.
- Any human-run comparison intended to support a claim.

It does not apply to local ad-hoc developer tests of harness correctness, provided those tests never enter the Evidence Ledger under an experiment ID.

---

## 2. Experiment Manifest Requirements

Every experiment **must** have a preregistered ExperimentManifest, stored and versioned before the first run is executed. The manifest must contain, at minimum:

### 2.1 Identity and Metadata
- `experiment_id`: Unique immutable identifier.
- `manifest_version`: Semantic version of the manifest.
- `date_preregistered`: ISO-8601 timestamp of manifest creation.
- `experiment_family`: Reference to the research-plan family (A–I) or a documented custom family.
- `research_questions`: Explicit list of questions the experiment is designed to answer, mapped to charter hypotheses (H1–H9) where applicable.
- `investigator`: Person or process responsible for the experiment design.

### 2.2 Design
- `hypotheses`: The specific statistical hypotheses (null and alternative) to be tested, written in terms of measurable quantities.
- `treatments`: Explicit list of system configurations being compared, each identified by:
  - Recipe ID/version (or "none" for cold-start).
  - Worker profile IDs/versions for each station.
  - Context policy ID/version for each station.
  - Gate policy ID/version.
  - DAG template ID/version.
  - Retry/escalation policy ID/version.
- `baselines`: Explicit list of baseline configurations (B0–B5 or documented custom) included in the comparison, with the same level of configuration detail as treatments.
- `unit_of_observation`: The unit over which outcomes are measured (task instance, station node, product run, DAG).
- `randomization_unit`: The unit of random assignment (if randomization is used).
- `sample_size`: Planned number of observations per treatment/baseline.
- `repetition_policy`: Whether and how the same task/product instance is repeated across identical configurations (see Section 11).
- `covariates`: Any variables (task difficulty, product class, station type, worker tier, etc.) that will be controlled for or stratified in analysis.
- `stopping_rule`: Explicit rule for when data collection stops. Optional-interim looks are allowed only if the stopping rule controls for multiple comparisons.

### 2.3 Task and Dataset Specification
- `task_set_id`: Reference to a versioned task/product dataset.
- `inclusion_criteria`: Explicit rules for which task instances are included.
- `exclusion_criteria`: Explicit rules for which task instances are excluded, with justification.
- `hidden_test_specification`: Description of hidden tests (see Section 3), including how they are isolated from model context.
- `task_ordering`: How task instances are ordered or randomized across runs.

### 2.4 Measurements
- `primary_outcomes`: List of primary endpoint metrics with operational definitions (e.g., "verified task success = all deterministic gates pass AND independent model reviewer approves with score >= X").
- `secondary_outcomes`: List of secondary metrics.
- `failure_taxonomy_mapping`: How outcomes are mapped to the charter's Failure Taxonomy categories.
- `measurement_provenance`: For each metric, which system component produces the measurement and how it is audited.

### 2.5 Statistical Analysis Plan
- `statistical_tests`: For each comparison, the specific test (e.g., two-sided binomial test for proportions, McNemar's test for paired binary data, t-test or non-parametric equivalent for continuous) and the test statistic.
- `acceptance_thresholds`: The specific numerical threshold for non-inferiority margins, superiority margins, and/or equivalence margins. **See Section 18 for pilot-threshold policy if thresholds are not yet established.**
- `significance_level`: The alpha level or false-discovery-rate control method. If adjusted, the adjustment method.
- `sensitivity_analyses`: Planned analyses testing robustness to assumptions (e.g., different non-inferiority margins, exclusion of outliers, different covariate specifications).
- `handling_of_missing_data`: Explicit rules for runs that crash, time out, or produce incomplete evidence.

### 2.6 Validity Controls
- Reference to the Threat Model (Section IDs) covering the specific threats this experiment's design addresses.
- Any threat that is acknowledged **not** to be fully mitigated must be listed with a documented limitation.

### 2.7 Manifest Immutability
- After the first run of an experiment executes, the manifest fields in Sections 2.2–2.6 **may not be changed**. Corrections to manifest typos, identity, or metadata are permitted if explicitly marked as corrections with a timestamp and justification.
- Design changes after execution begin create a new experiment ID; the original manifest is retained, and any runs already executed under the original design are analyzed and reported under the original experiment ID.

---

## 3. Baseline Requirements

### 3.1 Mandatory Baselines by Experiment Type

| Experiment Type | Mandatory Baselines |
|---|---|
| Product-level Pareto (S5, H6, H8) | B0 (Frontier Monolith), B1 (Fixed Multi-Agent), B3 (Factory No-Learning) |
| Recipe transfer (H5, H6) | B3 (Factory No-Learning) at minimum; B0 and B1 recommended |
| Worker downshift / MCW (H3, H7) | Same-station frontier-worker run as the control; B0 at product level if applicable |
| Station specialization (H1) | Same-model generalist-prompt control, context and tools equalized |
| Context isolation (H2) | Full-repo-context control with same worker and tools |
| Gate independence (H4) | Self-verification control using the producing worker |
| Evidence vs reputation (H9) | B2 (Cost Router / reputation-based selection) |
| Continuous optimization (H7) | B4 (Recipe fixed, no optimizer) |
| Drift / revalidation (H10) | Frozen recipe without revalidation as control |

### 3.2 Baseline Execution Parity
All baselines and treatments in an experiment must be executed under:
- Identical or explicitly documented-equivalent sandbox environments.
- Identical total budget caps (cost, tokens, wall-clock) where applicable. If a treatment has a structural advantage (e.g., parallelism), the baseline must be given a commensurate budget in terms of aggregate compute, and both wall-clock and aggregate compute are reported separately.
- Identical deterministic gates. Model-based reviewer gates may differ only if the reviewer identity is an explicit treatment variable; otherwise, the same reviewer configuration is used across all arms.
- Identical task instances, in the same random order (see Section 11).

### 3.3 Harness Calibration Baseline
Before any treatment-versus-baseline comparison is analyzed, the experiment must confirm that the harness itself operates correctly on a calibration subset:
- All recorded metrics are internally consistent.
- Replay of a recorded run reproduces the same gate outcomes (within documented tolerance for any non-determinism explicitly allowed by the manifest).
- The hidden-test isolation check passes (see Section 4).

Failure of harness calibration invalidates the entire experiment until the cause is identified and corrected. The harness-calibration runs are retained and reported as part of the experiment.

---

## 4. Hidden-Test Isolation

For any experiment that uses hidden tests as a primary or secondary outcome:

### 4.1 Structural Isolation
- Hidden test definitions, expected outputs, and scoring code **must not** reside in any file, prompt, context package, or runtime environment that is accessible to any producing worker.
- Hidden tests must be executed only by the Quality Gate Engine in a separate execution phase after the producing worker's workspace has been sealed and its outputs have been extracted.
- The Context Firewall must refuse to include any file matching hidden-test naming or path conventions, and this refusal must be logged.

### 4.2 Leakage Checks
- Each experiment manifest must define a **leakage-check procedure**: at minimum, a sample of recorded ContextManifest hashes is replayed and the reconstructed context packages are scanned for strings matching hidden-test content or structure.
- A **canary-token** check is recommended: unique non-functional tokens are inserted into hidden-test files at manifest creation time; the experiment automatically scans all worker outputs and context logs for any occurrence of these tokens. Any occurrence is leakage.
- Leakage detection is binary per evaluated item. If leakage is confirmed on any run, that run's affected task instances are marked as contaminated. Contaminated instances are **excluded from use as hidden evidence in the primary analysis and reported separately**.
- Experiment-level invalidation (if any) — e.g., discarding the entire experiment for its primary endpoint — **must be preregistered in the ExperimentManifest before the formal experiment begins**. Pilot work may, where justified, produce an operational tolerance recommendation recorded in a Pilot Threshold Report. No fixed percentage is asserted as a universal binding invalidation threshold across experiments.

---

## 5. Replayability

An experiment run is valid evidence only if it can be replayed.

### 5.1 Replay Criteria
A replay of a recorded run, executed using only the ExperimentManifest, stored ContextManifests, stored WorkerProfile versions, stored environment manifest, stored task dataset version, and the same REDI Lab software commit, must reproduce:
- The same sequence of lifecycle events.
- Identical deterministic gate results.
- Model-based gate results that are within the manifest's preregistered tolerance for LLM non-determinism. If no tolerance is preregistered, any divergence in model-based judgments requires a new repetition run to be added.
- The same FailureTaxonomy classification.

### 5.2 Replay Execution
- Replayability is **mandatory** for every experiment. The principle is not negotiable: an experiment run is valid evidence only if it can be replayed (Section 5.1).
- For every experiment, **at least a preregistered fraction of randomly selected recorded runs is replayed after data collection completes.** The replay fraction is preregistered in the ExperimentManifest before the formal experiment. Pilot experiments derive risk-tier-appropriate replay fractions: high-stakes B5-vs-B0 comparative experiments typically require a larger replay sample; harness-calibration and pilot-harness runs may use a smaller starting fraction.
- Every experiment, regardless of risk tier, replays **at minimum a small pragmatic floor of runs** as a provisional engineering safety default. This floor is a process-integrity baseline (initial minimum: 3 runs), not a scientifically validated statistical acceptance threshold. The appropriate floor per risk tier is refined through pilot experiments and recorded in Pilot Threshold Reports.
- Replay pass/fail is reported as an experiment-level quality metric.
- The replay-pass-rate criterion used to mark an experiment as "low-reproducibility" (and thus block promotion claims) is **preregistered per formal experiment** and derived from pilot calibration. Pilot experiments establish risk-tier-appropriate pass-rate expectations recorded in a Pilot Threshold Report. No fixed percentage is asserted as a universal binding pass-rate threshold across all experiments. If replay pass rate falls below the preregistered threshold for that experiment, the experiment is marked as low-reproducibility and no claims from it may be used for recipe promotion until the cause is identified and the affected runs are either explained or re-executed.

---

## 6. Environment and Version Capture

Every FactoryRun and node-level RunEvidence record must contain the following, captured at execution time and stored with the evidence:

### 6.1 Software Environment
- REDI Lab commit hash or exact package version.
- Python version and exact interpreter identifier.
- A full dependency manifest: for pip-based environments, the output of `pip freeze` or equivalent; for other environments, the equivalent locked dependency listing.
- Operating system identifier and version.
- Hardware identifier (CPU model, RAM, GPU if applicable).
- Clock skew and NTP status if timing is a primary outcome.

### 6.2 Runtime Adapter Environment
- For every adapter used: adapter name, adapter version, and any runtime-specific dependency versions.
- Sandbox identifier and version.
- Network configuration (allowed hosts, proxy configuration, DNS settings).
- Tool availability and versions for every tool permitted in the run.

### 6.3 Storage and Hashing
- The complete environment manifest must be hashed. The hash is stored with each RunEvidence record.
- Two runs are considered environment-identical only if their environment manifest hashes match and the task set version, recipe version, and all worker/context/gate policy versions also match.

---

## 7. Model, Provider, and Version Capture

### 7.1 Worker Identity Granularity
A WorkerProfile version must capture, at minimum:
- `provider`: Legal or contractual provider identity.
- `provider_endpoint`: Exact endpoint or API base URL used.
- `model_family`: Provider's model family identifier (e.g., "GPT-4o", "Claude Opus", "Gemini 1.5 Pro").
- `model_version`: Exact model version or snapshot identifier, if the provider exposes one. If the provider does not expose a stable version identifier, the date of the first run with this identity is recorded and the provider alias is flagged as "version-uncontrolled."
- `reasoning_configuration`: All reasoning-related parameters (temperature, top-p, reasoning effort level, max reasoning tokens, seed if provided, deterministic/non-deterministic mode).
- `generation_parameters`: max output tokens, stop sequences, response format constraints.
- `structured_output_mode`: Exact schema version if structured outputs are enforced.
- `tool_permissions_version`: Exact list of tools and their parameter constraints permitted to this worker identity.
- `context_limit`: Maximum input token budget.

### 7.2 Version-Uncontrolled Provider Aliases
If a provider does not expose a stable model snapshot identifier:
- All runs using that alias **must** be flagged in the Evidence Ledger as `model_version_uncontrolled: true`.
- Comparisons across time periods using version-uncontrolled aliases are treated as confounded by potential model drift, and the analysis must explicitly acknowledge this confound.
- Drift monitoring (see Section 17) runs with elevated frequency for version-uncontrolled workers.

### 7.3 Provider Behavior Snapshot
For every experiment run, the following additional metadata is captured from the provider where available:
- Provider-side request ID.
- Provider-reported token counts (prompt, completion, cached, reasoning, if exposed).
- Provider-reported model identifier echoed back in the response.
- Provider-side latency (time-to-first-token, total response time, if exposed).
- Any warning or deprecation flags in the provider response.

---

## 8. Context Manifest Capture

### 8.1 Exact Reproducibility Requirement
Every ContextManifest must contain:
- `context_manifest_id` and `version`.
- `station_contract_id`, `task_contract_id`, `product_spec_id`.
- A deterministic, ordered list of every artifact included in the context package:
  - For each file: file path, file content hash, file size (tokens and bytes), source revision/commit, and a selection rationale code.
  - For each upstream HandoffManifest: handoff ID and version hash.
  - For each contract or policy document: document ID and version hash.
  - For any summary or derived artifact: the algorithm/version used to produce it and the hashes of its source inputs.
- Exact token counts per artifact category (after tokenization by the specific worker's tokenizer, where available; fallback: a documented tokenizer with identity recorded).
- Total token count before sending to the worker.
- Any excluded artifacts with explicit exclusion rationale.
- `context_package_hash`: A deterministic hash of the entire assembled context package in its exact wire representation (after tokenization and formatting by the worker adapter).

### 8.2 Context Firewall Audit
- For every ContextManifest, the Context Firewall must also record:
  - Any scope-check rejections (which files were requested or inferred as relevant but excluded by the StationContract scope rules).
  - Any secret-scan hits and their disposition.
- A random sample (provisional engineering default audit rate: 5% of node runs) is audited post-hoc by reconstructing the context package from its manifest and verifying that the reconstructed hash matches the stored hash. This 5% rate is a process-integrity engineering default, not a scientifically validated research threshold. Higher-stakes experiments should preregister a larger audit fraction in the manifest. Mismatches are treated as evidence corruption.

---

## 9. Pricing and Cost Capture

### 9.1 Cost Schema
Every RunEvidence record must contain:
- `pricing_schedule_id`: Reference to a versioned document specifying the exact unit prices used for this run.
- `pricing_effective_date`: Date for which the pricing schedule was considered current.
- `input_cost_per_million_tokens`: Stated in the experiment's reference currency.
- `output_cost_per_million_tokens`: Same.
- `cached_input_cost_per_million_tokens`: If the provider distinguishes cached vs uncached prompt tokens.
- `reasoning_cost_per_million_tokens`: If the provider charges separately for reasoning tokens.
- `fixed_request_fee`: Per-call fee if any.
- `provider_billed_cost_total`: Actual billed or invoice amount, if available after the fact. Any discrepancy between estimated and billed cost is flagged.

### 9.2 Cost Calculation Rules
- Cost calculations are reproducible: given token counts and a PricingSchedule, anyone can recompute the estimated cost and get the same number.
- If a provider introduces discounts, credits, or tiers that affect effective pricing, this is recorded as a separate `pricing_adjustment` and the unadjusted list-price cost is also retained. Comparisons use list-price cost unless the manifest explicitly states otherwise, to avoid bias from transient pricing contracts.
- Cost is always reported separately for input, output, caching, reasoning, and fixed fees, so that analyses can recompute under different pricing assumptions.

### 9.3 Pricing Drift
- Any change in the pricing schedule creates a new `pricing_schedule_id`.
- Experiments spanning multiple pricing schedules either stratify analysis by schedule or explicitly acknowledge pricing drift as a confound.
- Historical evidence is never retroactively re-priced; both old and new schedules are retained, and analyses can compare under either.

---

## 10. Token Accounting

### 10.1 Required Token Counts
Every RunEvidence record must contain, at minimum:
- `input_tokens_provider_reported`: As echoed by the provider API, if available.
- `input_tokens_manifest_calculated`: Sum of token counts from the ContextManifest, computed before sending.
- `output_tokens_provider_reported`: If available.
- `output_tokens_measured`: Counted from the raw output by REDI Lab's tokenizer.
- `cached_input_tokens_provider_reported`: If exposed.
- `reasoning_tokens_provider_reported`: If exposed.
- `tool_call_tokens_provider_reported`: If exposed.
- `discrepancy_flag`: True if any of the corresponding counts diverge by more than the manifest's preregistered tolerance. A provisional engineering default tolerance of 2% may be used for flagging purposes until a pilot-derived tolerance is preregistered; this flag does not, on its own, invalidate the experiment's primary endpoint unless the manifest explicitly so specifies.

### 10.2 Discrepancy Handling
- Discrepancies exceeding the tolerance are flagged and investigated.
- If the discrepancy is systematic across a provider, the provider's reported counts are not used for cost or token-efficiency metrics; REDI-Lab-measured counts are used, and the discrepancy is reported as a limitation.

---

## 11. Wall-Clock Time versus Aggregate Compute

### 11.1 Required Timing Measurements
Every FactoryRun must report:
- `wall_clock_elapsed`: Real-world time from PRODUCT_ACCEPTED to PRODUCT_VERIFIED (or final failure), inclusive of queueing and scheduling.
- `aggregate_worker_compute_seconds`: Sum of the elapsed execution time of every WORKER_STARTED → WORKER_COMPLETED interval, including retries. If multiple workers run in parallel, they are summed, not maxed.
- `aggregate_gate_compute_seconds`: Sum of deterministic gate execution time and model-based reviewer execution time.
- `scheduler_idle_seconds`: Time spent with ready nodes waiting for available execution capacity.
- Per-node versions of all of the above.

### 11.2 Reporting and Interpretation
- For any comparison involving parallelism, both wall-clock and aggregate compute are reported as separate endpoints.
- No claim of "speed improvement" may cite only wall-clock time if aggregate compute increased; the tradeoff is explicitly discussed.
- If execution capacity (number of available parallel workers) varies between arms, this variation is either equalized by design or treated as a separate variable and reported. A treatment that uses more parallel capacity to reduce wall-clock time is a different claim from a treatment that reduces total compute.

---

## 12. Randomization and Repetition Policy

### 12.1 Randomization
- When task instances, worker assignments, or ordering are randomized, the random seed is recorded in the ExperimentManifest.
- The randomization method is documented: simple random, stratified random (with strata specified), blocked, or other.
- If an experiment does not randomize, the manifest must explicitly justify why a fixed order is acceptable and why ordering effects are not a plausible threat to validity.

### 12.2 Repetition
- For any experiment whose endpoints involve model-based gates or other non-deterministic components, each (task instance, treatment) pair is repeated a preregistered number of times (default: 3 repetitions for pilot experiments; larger N determined by pilot variance estimates before formal comparisons).
- The repetition count is chosen to achieve a preregistered statistical power or estimation precision target. If no target can be set pre-pilot, the experiment is marked as a pilot and a target N is established from its results (see Section 18).
- All repetitions are reported. No repetition may be discarded post-hoc except under the manifest's explicit exclusion rules, which must be documented preregistration.
- Within-repetition variance is reported alongside aggregate results.

---

## 13. Human Intervention Logging

### 13.1 Definition
A **human intervention** is any action taken by a human operator during a run that changes:
- The ProductSpec, TaskContract, DAG, or StationContract.
- The worker assignment, context package, gate policy, retry policy, or escalation decision.
- The run's state machine (e.g., manually marking a GATE_FAILED as passed, manually resuming a crashed run).
- The contents of any artifact produced by a worker before gates are applied.

### 13.2 Logging Requirements
Every human intervention must produce an `intervention_record` attached to the relevant RunEvidence or FactoryRun record, containing:
- Timestamp, operator identity.
- Exact state before and after the intervention (or a diff).
- Explicit intervention type code mapped to the categories above.
- Justification text from the operator.

### 13.3 Validity Consequences
- A run with any human intervention is **marked as non-autonomous**. It is retained in evidence.
- For primary-endpoint analyses of autonomy claims (e.g., "end-to-end verified with zero interventions"), non-autonomous runs count as failures.
- For analyses where autonomy is not the endpoint (e.g., pure quality analysis), intervened runs are either excluded from the per-protocol analysis or analyzed separately, and the impact on results is discussed.
- If an experiment's overall intervention rate exceeds a preregistered threshold (recorded in the ExperimentManifest before the formal experiment), any autonomy or cost claim from that experiment is considered unsupported unless the manifest explicitly justified the interventions as part of the protocol. A provisional engineering default concern rate of 10% of node attempts may be used by pilot work to flag experiments requiring additional review; this default is not a universal binding invalidation threshold and is refined by pilot experiments.

---

## 14. Retry Logging

### 14.1 Retry Record Schema
Every RETRY_REQUESTED event must produce a retry record attached to the originating RunEvidence record, containing:
- Original RunEvidence ID.
- Retry attempt index (1st retry, 2nd retry, ...).
- Failure taxonomy code of the original failure.
- Policy-under-which-retry-was-issued: retry rule ID/version, or manual flag.
- Any change between attempts (context rebuilt, worker escalated, DAG revised) identified explicitly by comparing the relevant manifest versions.
- Final outcome chain: a linked list of all attempts (initial + retries) for the same logical node.

### 14.2 Retry Budget and Analysis
- Every experiment manifest specifies a per-node retry budget. If retries exceed the budget, the node is a failure.
- Analyses may report both "per-attempt success rate" and "per-node eventual success rate (within budget)." The two are never conflated.
- Cost and token accounting includes all attempts (initial + all retries) for the node. A "success" that consumed three attempts and three worker calls is reported with its actual total cost, not the cost of the successful attempt only.

---

## 15. Failed-Run and Negative-Result Retention

### 15.1 Failed Runs
- **No failed, crashed, timed-out, budget-exhausted, or gate-rejected run may be deleted from the Evidence Ledger.** The ledger is append-only.
- Failed runs must have a FailureTaxonomy classification, a complete RunEvidence record as far as execution progressed, and any available debug output (worker traces, error messages, partial outputs).

### 15.2 Negative Results
- An experiment that fails to confirm its alternative hypothesis (or fails to reject its null) is a **negative result.** Negative results are retained, analyzed, and reported with the same rigor as positive results.
- The Evidence Ledger entry for a negative-result experiment must include:
  - The completed analysis.
  - Any planned sensitivity analyses.
  - A discussion section listing possible reasons (power, true null, implementation bug, confound).
- Summary documents citing any subset of experiments must explicitly list negative-result experiments alongside positive ones. Suppression of negative results is research misconduct under this policy.

### 15.3 Retention Period
- All evidence for completed experiments is retained for at minimum the lifetime of the REDI Lab research program, or longer if required by law or funding agreement.
- De-identification or aggregation of evidence is permitted for publication only if the raw evidence is retained and cross-referenceable.

---

## 16. Anti-Cherry-Picking Rules

### 16.1 Preregistration Enforcement
- The primary analysis plan in the ExperimentManifest (Section 2.5) is binding. Any additional analyses conducted after seeing the data are explicitly marked as **exploratory post-hoc analyses** and may not be used as evidence in support of any charter hypothesis, recipe promotion, or optimization decision.
- Exploratory analyses are retained and reported alongside primary analyses so readers can assess how many analyses were attempted.

### 16.2 Outcome Reporting
- For every experiment, **all** preregistered outcomes on **all** preregistered treatments and baselines are reported in the experiment's final report document. Omitting an arm, an outcome, or a task subset from the summary without explicit justification recorded in the manifest is prohibited.
- If the manifest defines a primary task subset, results on the full set are also reported (as secondary or sensitivity analysis).

### 16.3 Subgroup and Subset Analyses
- Subgroup analyses are exploratory unless the subgroup was specified as a stratification factor or interaction hypothesis in the preregistered design.
- If multiple thresholds or margins are investigated post-hoc, this is explicitly flagged and correction for multiple testing (e.g., Bonferroni, Benjamini–Hochberg) is applied before any claim is supported.

### 16.4 "Statistical Significance" Alone Is Insufficient
- A result may not be used for recipe promotion or cited as supporting a charter hypothesis solely on the basis of p < alpha. It must also:
  - Meet the preregistered effect-size or non-inferiority margin.
  - Survive the preregistered sensitivity analyses.
  - Have acceptable replay and leakage-check quality metrics (Sections 4, 5).
  - Have intervention and retry rates within acceptable bounds.

---

## 17. Preregistration Expectations for Important Comparisons

The following classes of experiment are considered **important comparisons** and require full preregistration (all manifest sections complete and signed off) **before any run executes**:

- Any experiment that will be used to support a Research Charter success criterion (S1–S7).
- Any experiment that will be used to support a Recipe Promotion (Section 19).
- Any experiment that will be used to support a Worker Downshift Decision (Section 20).
- Any external-publication-bound experiment.
- Any baseline-versus-B5 comparison on which a REDI-12 product/research decision may rest.

For these, the following additional rules apply:
1. The manifest is reviewed and signed off by at least one person who is not the principal designer of the experiment.
2. The manifest is stored in a version-controlled location with a timestamp that is auditable and cannot be backdated.
3. The final report references the manifest version, and any deviations between manifest and execution are itemized with their impact.

Pilot experiments whose sole purpose is to establish thresholds, variance, or sample size for a future important comparison are exempt from full statistical preregistration but must still have a manifest documenting their pilot purpose. Pilot results are not used directly as evidence for hypotheses or promotions.

---

## 18. Pilot Threshold Policy

For many statistical parameters (non-inferiority margins, acceptable failure rates, minimum effect sizes, sample sizes, required evidence coverage), scientifically justified thresholds cannot be fixed before REDI Lab has pilot data. This policy therefore establishes the following procedure:

### 18.1 Threshold Classification and Provenance

All numeric thresholds used in REDI Lab experiments are classified into four mutually exclusive categories. The category of every binding threshold must be documented alongside its rationale:

1. **Engineering Safety Limit (B)**. A process-integrity or operational default (e.g., a minimum audit sample fraction, a sanity-flag tolerance, a pragmatic floor of runs to be replayed). These are governance or process design choices, not scientific acceptance criteria. They may be set as provisional internal defaults before pilot work and refined as operational experience accumulates. Engineering Safety Limits do not, on their own, determine pass/fail of a scientific hypothesis.

2. **Scientific Acceptance Threshold**. A threshold that directly determines whether a hypothesis is considered supported or falsified, or whether a recipe/downshift is promoted or rejected. Example: a non-inferiority margin, a minimum effect size, or an experiment-level contamination invalidation fraction.

3. **Pilot-Calibrated Threshold (C)**. A Scientific Acceptance Threshold whose numerical value is determined by a dedicated pilot experiment whose sole purpose is variance estimation and margin calibration. The resulting value is recorded in an approved Pilot Threshold Report and preregistered in the formal experiment's manifest before the formal experiment runs.

4. **Externally Justified Threshold (A)**. A Scientific Acceptance Threshold whose numerical value is derived directly from:
   - Published peer-reviewed literature on comparable systems, or
   - A documented, versioned cost-model that explicitly maps the threshold to an operational meaning (e.g., "a downshift must save at least X% to offset the revalidation cost"). The cost-model document is referenced by the ExperimentManifest.

**Normative Rule**: No numeric scientific acceptance threshold may become binding unless its rationale is recorded in the ExperimentManifest or an approved Pilot Threshold Report before the formal experiment begins.

No numeric acceptance threshold, margin, sample size, or statistical power target is asserted as fact in this charter, the experiment policy, or the research plan unless it is classified as an Externally Justified Threshold (category 4 above). If none of the above sources are available, the parameter is left as **to be established by pilot** (category 3).

### 18.2 Pilot Experiment Program
- Before any formal hypothesis test supporting H1–H9 or S1–S7, the corresponding Experiment Family must first run one or more explicitly marked pilot experiments whose only purpose is to:
  - Estimate outcome variances, success-rate baselines, and effect-size distributions.
  - Estimate drift, leakage, and intervention rates.
  - Produce recommended values for: non-inferiority/superiority margins, required sample size per arm, minimum repetition count per cell, maximum acceptable leakage/replay-failure/intervention rates, and minimum evidence coverage for recipe validity.
- Pilot recommendations are documented in a Pilot Threshold Report, which is reviewed, versioned, and then **preregistered as the threshold package** for the subsequent formal experiment. The formal experiment's manifest then references this threshold report.

### 18.3 Thresholds Cannot Be Re-Tuned Post-Hoc
- Once a threshold package is preregistered for a formal experiment, it is fixed for that experiment. If pilot results suggest the thresholds were wrong, a new experiment with a new threshold package is planned; the original experiment's results are analyzed against the original thresholds and retained.

---

## 19. Recipe Promotion Evidence Requirements

A RecipeCandidate may be promoted to a validated production recipe only if **all** of the following conditions are met:

### 19.1 Evidence Quantity
- The candidate recipe has been evaluated on at least a preregistered minimum number of distinct, independent product instances in the target class. (Minimum N is to be established by pilot calibration per Section 18 and preregistered in the ExperimentManifest before formal promotion evaluation. The REDI_LAB_RESEARCH_PLAN v0.2 baseline establishes the qualitative principle that a single successful run is insufficient to declare a stable recipe, but does not specify a numeric minimum count of sibling products before candidate generation.)
- Evidence coverage (fraction of recipe fields that have direct empirical backing from recorded runs, as opposed to defaults or heuristics) meets or exceeds the preregistered threshold.

### 19.2 Quality Non-Regression
- The candidate recipe, when executed on held-out sibling products, achieves a verified-quality endpoint that is statistically non-inferior to the current validated recipe (or, for the first recipe in a class, to the cold-start baseline B3) at the preregistered non-inferiority margin.
- No individual quality-gate failure rate regresses beyond the preregistered bound.

### 19.3 Cost / Latency / Frontier-Usage Claims
- If the candidate recipe claims an improvement in cost, wall-clock, or frontier-model token share, this improvement is:
  - Statistically significant at the preregistered alpha, AND
  - Larger than the preregistered minimum-effect-size threshold for operational meaningfulness, AND
  - Robust to the preregistered sensitivity analyses.

### 19.4 Reproducibility and Audit
- The replay pass rate on candidate-recipe runs meets the experiment policy's threshold (Section 5).
- Hidden-test leakage checks pass.
- Intervention rate is within bounds (Section 13).
- A human review sign-off has confirmed that the recipe's fields are consistent with evidence (i.e., no heuristic defaults are mislabeled as evidence-backed).

### 19.5 Rollback
- Every promoted recipe retains a rollback path: the previously validated recipe version remains available and can be activated immediately if the newly promoted recipe shows post-promotion regression.
- Promotion is an explicit versioned event in the Recipe Store, with full audit trail (experiment IDs cited, sign-off identities, timestamp).

### 19.6 Gradual Promotion Ladder
Recipes are promoted through a ladder, not binary flipped:
1. **Candidate**: Generated from evidence, not used for production mode.
2. **Shadow**: Executed alongside the current validated recipe on a fraction of production runs, outputs compared but not used for acceptance.
3. **Canary**: Used for a bounded fraction of real production runs with elevated monitoring and automatic rollback on regression.
4. **Validated**: Full production mode.

The minimum duration and sample size for each rung is preregistered in the promotion protocol. Skipping rungs is prohibited unless explicitly justified by an emergency (e.g., the current validated recipe has become unusable due to model/provider failure).

---

## 20. Worker Downshift Evidence Requirements

A proposal to replace a station's default worker with a smaller/cheaper worker or to reduce a station's context budget is a **downshift**. Downshifts are subject to the following evidence requirements:

### 20.1 Preregistered Downshift Experiment
- The downshift is the subject of an ExperimentManifest comparing:
  - ARM A: Current worker + current context budget (control).
  - ARM B: Proposed smaller worker and/or reduced context budget (treatment).
- The experiment is conducted on a representative sample of the station's task class, drawn from recent historical tasks (to match actual workload distribution) and/or new held-out tasks, with the composition documented.

### 20.2 Non-Inferiority on Quality
- Verified task success rate of ARM B is statistically non-inferior to ARM A at the preregistered margin. The margin for a per-station downshift is **to be established by pilot** (per Section 18 threshold policy).
- No individual deterministic-gate failure category shows a preregistered-worsening.

### 20.3 Retry and Escalation Accounting
- The comparison uses **end-to-end node cost and latency including retries and escalations.** If a downshifted worker initially succeeds less often but triggers more retries and escalations, the full node cost (retries + stronger worker) is attributed to the downshifted arm. A downshift that saves on first-attempt cost but increases total node cost is rejected.

### 20.4 Sample Size and Repetitions
- Sample size and per-cell repetition count are chosen to achieve preregistered power for the non-inferiority test.
- If the task class has subcategories with materially different difficulty, the experiment stratifies accordingly.

### 20.5 Post-Promotion Monitoring
- After a downshift is adopted into a recipe, the station continues to operate under an elevated monitoring regime for a preregistered window. Any statistically significant regression in success rate during this window triggers automatic rollback to the previous worker/context configuration and opens a revalidation experiment.

---

## 21. Drift and Revalidation Rules

### 21.1 Drift Detection Triggers
Revalidation is triggered automatically, or manually by investigators, when any of the following occur and the affected station/recipe evidence exists:
- A provider reports a model-version change, deprecation, or retirement.
- A WorkerProfile flagged as `model_version_uncontrolled` (Section 7.2) shows a statistically significant change in any primary metric on a standard calibration task set, compared to its historical baseline.
- A pricing schedule change materially alters the cost ranking of candidate workers.
- An environment dependency (runtime adapter, sandbox, tool version, REDI Lab core version) changes in a way that could plausibly affect outcomes.
- Quality, cost, or retry metrics for a validated recipe drift beyond a preregistered control-chart bound for a sustained window.
- A new candidate worker (new model, new provider, new pricing tier) becomes available that could plausibly downshift one or more stations.

### 21.2 Revalidation Scope
- Revalidation is **targeted.** Only stations and recipe fields plausibly affected by the drift trigger are revalidated first. If targeted revalidation passes, the rest of the recipe is assumed stable unless evidence contradicts this assumption.
- The rationale for which stations are in-scope versus out-of-scope for a given drift event is documented and versioned alongside the revalidation experiment.

### 21.3 Revalidation Freeze
- While a drift-triggered revalidation is in progress for a recipe:
  - Automatic promotion of new recipe candidates derived from post-drift evidence is frozen.
  - Existing validated recipes continue to be used, but with elevated monitoring.
  - Any new evidence collected during the freeze is analyzed both:
    - Against the frozen (pre-drift) statistical baselines (to detect drift magnitude), AND
    - Separately as part of a new post-drift baseline (once drift is confirmed).

### 21.4 Outcomes
A revalidation has three possible outcomes:
1. **Retain current recipe**: No statistically or operationally meaningful regression detected. Recipe remains validated; drift marker is recorded but otherwise no change.
2. **Roll back**: Current recipe is confirmed to have regressed. Rollback to the previous validated recipe; current recipe is demoted to Candidate or Shadow pending correction.
3. **Recipe revision required**: Drift magnitude is large enough that the recipe must be revised. A new RecipeCandidate is generated using post-drift evidence and promoted through the ladder in Section 19.6.

All three outcomes are recorded in the Evidence Ledger and Recipe Store with experiment citations.

---

## 22. Experiment Validity Summary Checklist

A REDI Lab experiment is valid for evidential use only if all items below are true (or explicitly marked N/A with justification in the manifest):

- [ ] ExperimentManifest is complete per Section 2 and preregistered before first run.
- [ ] All mandatory baselines per Section 3 are included with execution parity.
- [ ] Harness calibration baseline passes.
- [ ] Hidden-test isolation checks pass. Leakage below threshold.
- [ ] Replay sample meets replay pass rate threshold.
- [ ] Full environment/version manifests captured per Section 6.
- [ ] Worker identities captured at the granularity of Section 7.
- [ ] Context manifests captured and a sample hash-audited.
- [ ] Pricing/cost and token accounting captured per Sections 9 and 10.
- [ ] Wall-clock and aggregate compute both reported where relevant.
- [ ] Randomization seed and method documented; repetition count meets power target.
- [ ] All human interventions logged; intervention rate within bound.
- [ ] All retries logged; full attempt chains counted in cost/quality.
- [ ] Failed runs and negative results retained.
- [ ] Anti-cherry-picking: all arms, all outcomes, full task set reported; primary analysis matches manifest.
- [ ] Preregistration: important comparisons fully preregistered and signed off.
- [ ] Statistical thresholds (margins, sample size, alpha) were established by pilot or literature, not invented.
- [ ] If used for promotion/downshift: Sections 19 or 20 criteria met.
- [ ] If triggered by drift: Section 21 procedure followed.
