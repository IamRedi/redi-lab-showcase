# REDI Lab Threat Model

Document version: 1.0
Date: 2026-09-15
Milestone: REDI-00A
Status: Frozen pending REDI-00A validation

---

## 1. Introduction

This document enumerates the major threats to which REDI Lab is exposed during its research program. Two categories of threat are addressed:

1. **Security Threats**: Threats to the confidentiality, integrity, or availability of REDI Lab systems, data, and the systems on which REDI Lab operates.
2. **Research Validity Threats**: Threats to the soundness of REDI Lab's experimental results and the conclusions drawn from the Evidence Ledger. A system can be perfectly secure while producing completely invalid research, so these threats are treated as a first-class concern on equal footing with security.

For every major threat, the document defines:
- **Threat**: A description of the adverse event.
- **Impact**: The harm that would result if the threat materializes.
- **Mitigation**: The procedural, architectural, or technical controls that REDI Lab applies.
- **Residual Risk**: The level of risk remaining after mitigations are applied, rated on a qualitative scale: **Low / Medium / High / Critical**. Residual risk is never Zero; mitigations reduce but do not eliminate risk.

Threats are not exhaustive. They represent the major known categories that the REDI Lab architecture and policies are designed to defend against. New threats discovered during the research program are appended via version updates (see Section 4, Change Control).

---

## PART A: SECURITY THREATS

---

### S-01: Prompt Injection from Repository Content

| Field | Description |
|---|---|
| **Threat** | Malicious or misleading text embedded in repository files (source code, comments, README, configuration, .env files, issue descriptions, commit messages) instructs the worker or orchestrator to ignore its system prompt, leak secrets, exfiltrate data, execute unauthorized actions, or produce incorrect outputs. |
| **Impact** | Confidentiality: secret or private-data leakage. Integrity: incorrect product outputs, false gate passes, corrupted evidence, false recipe promotion. Availability: resource exhaustion via injected loops or expensive operations. Severity escalates with the trust level of the repository: a customer codebase (D05) can cause downstream harm to the customer if the factory produces compromised code. |
| **Mitigation** | (1) Station Contracts define owned scope explicitly; the Context Firewall includes only files required by the contract and excludes advisory-only files by default. (2) System prompts and Station Contracts are prepended with high-priority instructions that override user-content directives; specific phrasing and bypass testing are part of the V0 security review. (3) Deterministic quality gates (build, test, lint, scope checks, secret scans) do not depend on model acceptance of injected instructions; they are enforced by the runtime. (4) Reviewer nodes receive only the artifact and evaluation criteria, not the builder's private input context; an injection that succeeds against the builder must independently succeed against the reviewer to bypass gates. (5) Tool execution is allowlisted per station; injected tool requests for disallowed tools are rejected by the runtime adapter. (6) Repository-content provenance is recorded in the ContextManifest; if an injection is detected post-hoc, affected runs are flagged in the Evidence Ledger. |
| **Residual Risk** | **Medium**: New prompt-injection techniques and edge cases continue to emerge. Defense-in-depth (deterministic gates, independent reviewers, tool allowlists) limits the blast radius of any single successful injection against a worker, but the possibility of coordinated injections across builder and reviewer or of injections that exploit the deterministic gate logic itself remains non-zero. |

---

### S-02: Malicious Dependency / Project Instructions

| Field | Description |
|---|---|
| **Threat** | Build dependencies (npm/pip/cargo packages, Docker images, transitive deps), CI configs, Makefiles, or project instructions execute arbitrary code during the build or test phase of a product, either through typosquatting, compromised upstream versions, or malicious specifications in the Product Order. |
| **Impact** | Compromise of the worker sandbox or sandbox escape; arbitrary code execution under the runtime's identity; unauthorized network access; tampering with Evidence Ledger or Recipe Store if the sandbox shares storage with the control plane; secret theft if the runtime has access to Tier-2/3 data it should not. |
| **Mitigation** | (1) Every Worker executes in an isolated sandbox with: no access to the REDI Lab control plane's internal storage, no default network access (network explicitly enabled per station only), no access to secrets beyond the single task's tool-specific injected secrets, ephemeral filesystem destroyed after the node completes. (2) Deterministic gates run in a separate sandbox from the builder; a dependency that executes during build and tampers with builder output cannot simultaneously tamper with the gate runner unless the gate runner also uses the dependency. (3) Third-party dependencies fetched during a build are, by V0 default, pinned by exact hash from a curated allowlist of trusted registries; deviation from the allowlist causes the deterministic gate `dependency_integrity_check` to fail unless explicitly overridden with a recorded architectural decision. (4) Execution Runtime adapter logs all process launches, file modifications, and network calls within the sandbox; anomaly detection flags deviations from a baseline for the station/task class. (5) The control plane (orchestrator, ledger, store, optimizer) runs in a separate security domain from any worker sandbox; sandbox processes cannot write to ledger or store databases directly—they can only emit events through the Event Router, which applies schema validation and auth. |
| **Residual Risk** | **Medium–High**: Sandbox escapes and zero-day vulnerabilities in language package managers are not uncommon. Defense-in-depth and strong control-plane isolation limit blast radius, but a determined attacker with a zero-day in both the sandbox and a dependency might compromise a single-node run. Broader compromise of the control plane is prevented by the separate security domain. |

---

### S-03: Secret Leakage

| Field | Description |
|---|---|
| **Threat** | Credentials, API keys, tokens, private keys, or other D11-class material is exposed in prompts, context packages, model outputs, execution traces, logs, commit history, Evidence Ledger rows, or Recipe Store entries. |
| **Impact** | Unauthorized access to provider APIs (runaway cost, data access), source-code repositories, customer systems, or REDI Lab infrastructure. Reputational and legal harm, potential breach notification obligations. |
| **Mitigation** | (1) Data Policy Section 6 (D11) rules are enforced by: (a) dedicated secret storage, never filesystem or env files; (b) runtime-only injection into tool execution environments, never into model context; (c) secret scanning at multiple ingest points (context package send, model output ingest, execution trace ingest, repository commit hooks) with regex, entropy, and known-secret-fingerprint detectors; (d) immediate abort and quarantining on scan hit. (2) Station Contracts explicitly forbid tools from echoing secrets in return values, and the adapter layer strips any response field that matches known secret patterns before the value reaches the worker context. (3) Evidence Ledger rows are structured to have typed, non-secret fields; raw traces containing tool outputs are stored separately (Tier 2/4) and never rendered directly in any public report without D12 review. (4) Regular secret rotation and incident-response procedures documented in Data Policy Section 11.4. |
| **Residual Risk** | **Low–Medium**: High-entropy secret scanning is reliable for standard formats; non-standard or low-entropy secrets (e.g., a custom bearer token with weak format) might evade pattern detection. Human process failures (operator copying a secret into a prompt, misconfigured CI variable) remain a non-zero residual risk. |

---

### S-04: Destructive Tools

| Field | Description |
|---|---|
| **Threat** | A worker invokes a tool that deletes files, drops database tables, terminates cloud resources, overwrites production data, or executes destructive filesystem operations outside its assigned scope. |
| **Impact** | Data loss inside a sandbox (low impact, sandbox is ephemeral); data loss in shared storage or connected external systems (high impact); accidental destruction of customer data or REDI Lab evidence. |
| **Mitigation** | (1) Station Contracts include an explicit allowed-tool list; disallowed tools are rejected by the runtime adapter before the call reaches the model's provider. (2) Tools that modify state outside the sandbox are classified by risk: by default, V0 allows NO tool that modifies external state. Any such tool added in later milestones is behind a risk-tier gate that requires explicit per-product approval and rollback capability (e.g., a write tool that first snapshots state and can revert). (3) The sandbox has a copy-on-write filesystem layer; modifications within the sandbox are applied to an ephemeral layer, and the original inputs are preserved in read-only storage. After the node completes, the ephemeral layer is discarded unless explicit handoff artifacts are extracted through the HandoffManifest schema. (4) Hard timeouts and budget limits prevent a destructive-loop tool call from consuming unbounded resources or time. |
| **Residual Risk** | **Low** for V0 (no destructive external tools); **Medium** for future milestones if external-state-modifying tools are added. The risk can be managed but never eliminated entirely—every state-modifying tool has failure modes. |

---

### S-05: Filesystem Escape

| Field | Description |
|---|---|
| **Threat** | A worker, through a tool call, malicious generated code, or an exploited dependency, reads or writes files outside the sandbox's allowed scope. |
| **Impact** | Read access: disclosure of private data, secrets, or other workspaces' content. Write access: tampering with evidence, cross-contaminating other product runs, persistence of malware. |
| **Mitigation** | (1) Sandbox abstraction defines a strict filesystem root per worker run; the runtime adapter uses OS-level mechanisms (mount namespaces, chroot, virtual filesystem, or the sandbox tooling provided by the adapter) to enforce that the process cannot access paths outside the allowed tree. (2) All tool file operations pass through a path-allowlist filter that rejects absolute paths outside the sandbox root and resolves relative paths before allowing the call. (3) Per-workspace isolation: each node run gets its own sandbox instance; no two node runs share filesystem state unless explicitly mediated by the HandoffManifest import step, which validates schema and runs scope checks before writing to the downstream workspace. (4) The runtime adapter audits every file operation and logs path, operation, and size. |
| **Residual Risk** | **Medium**: OS-level sandbox escapes, while uncommon, are possible. The separate security domain for the control plane and the ephemeral nature of per-node sandboxes limit the impact of a single escape to that node's workspace lifetime. |

---

### S-06: Network Abuse

| Field | Description |
|---|---|
| **Threat** | A worker uses network access to exfiltrate private data, scan internal networks, attack external targets, mine cryptocurrency, download unvetted payloads, or consume unbounded bandwidth. |
| **Impact** | Data exfiltration of D04/D05/D11; participation in botnet activity; legal liability for attacks originating from REDI Lab infrastructure; cost and resource exhaustion. |
| **Mitigation** | (1) Network is **denied by default.** A StationContract must explicitly request network access for specific hosts/protocols; otherwise the sandbox has no network interface. (2) When network is enabled, an explicit allowlist of hosts and protocols is enforced by the sandbox runtime and by the Event Router at the control-plane level. (3) Bandwidth caps, connection rate limits, and per-call timeouts are enforced. (4) Outbound traffic is logged and analyzed for anomalous patterns (unexpected hosts, upload-heavy patterns, DNS tunneling indicators). (5) Downloads are subject to hash-pinning or signature-verification rules where applicable (see S-02). |
| **Residual Risk** | **Low–Medium**: Allowlisting prevents casual abuse, but a legitimate host on the allowlist could itself be compromised or host a malicious subdomain. Bandwidth and rate limits prevent resource exhaustion but not all exfiltration of small data volumes through clever encoding. |

---

### S-07: Privilege Escalation

| Field | Description |
|---|---|
| **Threat** | A worker, reviewer node, or external attacker escalates from the privileges of a single sandboxed node run to obtain control-plane (orchestrator, ledger, store, optimizer) privileges, or from a low-tier station to a high-tier station with broader tool access. |
| **Impact** | Integrity: Evidence Ledger tampering, silent recipe mutation, forged gate passes, forged promotion approvals. Confidentiality: access to Tier-2/3/4 data from a node that should only see Tier-1 data. Availability: denial of service on the control plane. |
| **Mitigation** | (1) Strict separation of security domains: worker nodes run in a sandbox identity that has NO direct access to control-plane databases. They communicate only with the Event Router via a well-defined event schema and a per-run scoped token that grants emit-only rights for events belonging to that specific node ID. (2) All control-plane operations (writing evidence, promoting recipes, marking gates passed) require a distinct control-plane identity with role-based access control: orchestrator identity handles management decisions; gate engine identity writes GateResults; evidence-ingestor identity writes RunEvidence; recipe-approver identity promotes recipes. No single identity has blanket write access. (3) Station tool-permission scopes are strictly defined in the StationContract; the runtime rejects an attempt by a Station-A worker to invoke a tool reserved for Station B, even if the same physical process is reused. (4) The Event Router validates every incoming event's schema, identity, and scope against the run it belongs to before forwarding the event. Invalid events are dropped and logged. (5) Audit log of all control-plane actions, tamper-evident where possible. |
| **Residual Risk** | **Medium**: A vulnerability in the Event Router's schema validation or token validation, or a logic error in the RBAC implementation, could enable escalation. Defense-in-depth via layered controls (identity + schema + per-event scope + audit logs) raises the bar but cannot guarantee zero risk. |

---

### S-08: Unauthorized Scope Changes

| Field | Description |
|---|---|
| **Threat** | A Product Order, TaskContract, or DAG is modified mid-run—by a bug, a malicious worker, or an operator error—to include requirements or stations that were not in the original authorized scope, causing the factory to produce code, access data, or invoke tools outside the approved authorization for that run. |
| **Impact** | Confidentiality: accessing private repositories or tools not approved for the product. Integrity: producing a product that does not match the order, potentially introducing malicious or out-of-contract features. Legal/contractual: violating customer scope agreements or data-licensing limits. |
| **Mitigation** | (1) ProductSpec, ProductDAG, and all TaskContracts and StationContracts are versioned and signed (logically at minimum, cryptographically as V0 matures). A runtime change to any of these objects is recorded as an explicit **graph revision** or **contract revision** event, identity-attributed, and cannot happen silently. (2) Scope-expanding revisions require either an architectural decision event (for the orchestrator to approve) or an explicit human intervention record (for operator approval). Scope-restricting revisions are allowed automatically but still versioned. (3) Every StationContract's owned-scope and forbidden-scope clauses are enforced by the Context Firewall and the tool allowlist; even if a DAG revision tries to sneak in a new scope, the station's contract at execution time does not permit the relevant files or tools. (4) Final PRODUCT_VERIFIED includes a scope-comparison check that the set of artifacts produced and stations invoked lies within the ProductSpec's scope (or its explicitly approved revisions). Scope violations that reach final acceptance cause a gate failure and incident review. |
| **Residual Risk** | **Low–Medium**: The main residual risk is a logic error in the scope-expansion-approval code path that incorrectly treats a scope-expanding revision as scope-restricting, or a missing check in the final acceptance gate. Comprehensive unit and integration tests at REDI-02/03 mitigate this. |

---

### S-09: Unsafe Parallel Workers

| Field | Description |
|---|---|
| **Threat** | Two or more node runs executing in parallel on a product interfere with each other: concurrent writes to shared state, race conditions on integration handoffs, one worker reading another's unapproved intermediate output and propagating errors or injected content, resource contention causing cascading timeouts. |
| **Impact** | Integrity: corrupt HandoffManifests, merge conflicts producing incorrect integration, subtle data races that only manifest on specific parallel schedules. Availability: deadlocks, livelocks, cascading budget exhaustion. Research validity: spurious failures attributed to the wrong worker or configuration. |
| **Mitigation** | (1) Parallelism is derived exclusively from the Product DAG dependency structure. Nodes execute concurrently only if they are mutually independent in the DAG; dependent nodes cannot start. This guarantee is enforced by the Scheduler. (2) Each node has a fully isolated workspace with no shared mutable filesystem state. Upstream outputs pass to downstream consumers ONLY through the HandoffManifest mechanism, which: (a) is schema-validated, (b) passes through deterministic integrity checks (hashes of included artifacts), (c) is explicitly imported into the downstream workspace by the Context Compiler, never auto-mounted. (3) Integration nodes enforce deterministic merge ordering and explicit conflict-detection gates: any merge conflict or cross-branch interface mismatch triggers INTEGRATION_FAILED and a structured conflict report, not a silent best-effort merge. (4) Resource isolation: each node sandbox has independent CPU, memory, disk, and (where applicable) GPU limits; a resource-heavy node cannot starve its siblings. (5) Parallelism events are recorded with timestamps at nanosecond granularity where available; the Event Router sequences event delivery deterministically so the ledger shows a total order consistent with the wall-clock partial order. |
| **Residual Risk** | **Low–Medium**: The main residual risk is non-deterministic integration flakiness due to subtle interface mismatches that the deterministic merge check does not catch, but which appear only under certain parallel schedules. Recording full timing and rerunning with sequential ordering (as a sensitivity analysis in experiments) helps diagnose such cases. |

---

### S-10: Cross-Workspace Contamination

| Field | Description |
|---|---|
| **Threat** | Data, state, or artifacts from one product run (or one customer's project) leaks into the workspace of a different product run (or different customer). |
| **Impact** | Confidentiality: customer data leakage across tenants. Integrity: a recipe trained on Customer A's code is inappropriately warm-started for Customer B's unrelated product, producing incorrect or copyright-violating outputs. Research validity: experimental results are confounded by hidden carryover effects between runs. |
| **Mitigation** | (1) Workspace isolation is per-node, per-run, and per-product: no files, caches, or in-memory state persist across product runs unless explicitly mediated by the Recipe Store (which stores only versioned, de-contextualized recipes, not raw code). (2) Recipe Store entries record the exact product-class signature, evidence-set IDs, and license scope of their source; the Factory Configurator refuses to warm-start a product with a recipe whose license or customer scope does not match the new product. (3) Tiered storage (Data Policy Section 5.1) physically or logically separates customer-isolated Tier-4 data from cross-customer analytics. (4) Context Compilation never includes files or handoffs from a different product run unless explicitly referenced as a public-library dependency with the correct provenance. (5) In experiments, the randomization order of task instances across treatments and runs protects against systematic carryover; the ExperimentManifest must record workspace ID per run. |
| **Residual Risk** | **Low** for V0 (one product at a time). **Medium** for future multi-tenant or multi-product execution: the Recipe Store's scope-checking logic must correctly implement licensing and customer-scoping rules, and mistakes here could create subtle contamination. Regular compliance audits (Data Policy Section 13) mitigate this. |

---

### S-11: Malicious Generated Code

| Field | Description |
|---|---|
| **Threat** | The code produced by a Builder worker node contains backdoors, logic bombs, data-exfiltration endpoints, privilege-escalation primitives, or unsafe patterns that, when executed in the product's target environment, cause harm. |
| **Impact** | End-user harm if the product is deployed; downstream liability for REDI Lab or its customers; reputational damage; potential violation of security regulations. |
| **Mitigation** | (1) Station Contracts for Builder explicitly forbid producing security-relevant functionality (authentication, authorization, payment processing, secret handling) in V0 unless the Product Order explicitly includes a security-review station with dedicated gates. (2) Deterministic gates include: secret scan, dependency-integrity check, static-analysis rules covering OWASP Top-10 categories where tooling exists, allowed-file-scope check (no writes outside permitted paths in the generated codebase). (3) Independent Reviewer station receives the artifact with explicit security-review criteria and a separate context; Reviewer rejection fails the gate. (4) Security-focused gate station can be added to any recipe where security sensitivity exceeds a threshold. (5) Produced code never runs with elevated privileges in any REDI Lab environment; test execution runs in the same restricted sandbox as the builder. (6) Final gate policy on security-sensitive Product Orders requires a documented human security audit sign-off before PRODUCT_VERIFIED if the product class falls above a security-risk threshold (to be defined in a later milestone). (7) REDI Lab makes NO representation that generated code is production-safe without a separate independent security audit. This is documented in all product-level communications and is a Research Charter non-goal (Section 11.3). |
| **Residual Risk** | **High**: Malicious-code detection in general is undecidable. Even with multiple gates and human review, a sophisticated adversary can plant backdoors that evade static analysis, pass tests, and appear benign to human reviewers. REDI Lab's mitigations reduce the probability and raise the bar but do not eliminate this risk. The non-representation clause and customer-side audit requirements are part of the residual-risk management strategy. |

---

### S-12: Compromised External Tools / Providers

| Field | Description |
|---|---|
| **Threat** | A model provider (LLM API), external tool API, runtime dependency, or sandbox provider behaves maliciously or is compromised: returns poisoned outputs, silently tampers with tool arguments or return values, exfiltrates data sent to it, or lies about model identity/version. |
| **Impact** | Integrity: false gate passes, incorrect products, corrupted Evidence Ledger if the provider's reported token counts or model IDs are fabricated. Confidentiality: data sent in prompts or tool calls is exfiltrated by the provider. Availability: widespread provider outage or rate limiting halts all research progress. Research validity: if a provider silently upgrades a model and does not expose a version identifier, "identical worker identity" runs across weeks are actually on different models, invalidating longitudinal comparisons. |
| **Mitigation** | (1) Provider independence in the architecture: no domain object depends on provider-specific types (Architecture Document Section 4). Providers are behind adapters; switching providers does not require changes to the REDI Method layer. (2) Exact provider response metadata (provider request ID, echoed model ID, timestamps, provider-reported tokens) is captured per RunEvidence (Experiment Policy Section 7.3) and audited; anomalies are flagged. (3) Version-uncontrolled provider aliases are flagged in the WorkerProfile and trigger elevated drift monitoring and more frequent revalidation (Experiment Policy Section 21). (4) Cross-provider validation experiments: important Recipe Promotions and Downshift Decisions require, where feasible, that at least one arm of the experiment is replicated on a second, independent provider to rule out provider-specific artifacts. (5) Tool return values pass through schema and sanity checks in the adapter before being handed to the worker; implausible return values (wrong type, obviously poisoned data) cause the node run to fail with tool-failure taxonomy rather than propagating the bad data. (6) Business-contractual protections (data-processing agreements, security audits of critical providers) are obtained before any D04/D05 data is sent to a provider. No D04/D05 customer data is sent to any provider without a written DPA. |
| **Residual Risk** | **Medium–High**: Providers are black-box third parties. REDI Lab cannot verify internal execution inside the provider. Mitigations focus on detection, containment, and cross-validation rather than prevention, which is outside REDI Lab's control. For public-task research (D01/D02/D03), provider compromise primarily threatens validity rather than confidentiality. |

---

## PART B: RESEARCH VALIDITY THREATS

---

### R-01: Evaluator / Model-Family Correlation

| Field | Description |
|---|---|
| **Threat** | The Builder worker and the independent Reviewer worker (or the deterministic-gate author and the builder) are drawn from the same model family, training data distribution, or provider fine-tuning cohort. As a result, they share correlated blind spots: a systematic error made by the builder is also missed by the reviewer, inflating the apparent pass rate of model-based gates. |
| **Impact** | Invalid quality metrics; false acceptance of defective outputs (Type II error in gate decisions); overestimation of REDI Lab's verified quality; Recipe Promotion based on inflated quality evidence; spurious confirmation of H4. |
| **Mitigation** | (1) Experiment Policy Section 3.2 requires identical gate configurations across arms; within a single experiment, reviewer identity is constant so correlations affect all arms equally (preserving relative validity, though inflating absolute pass rates). (2) For absolute-validity claims and for final Recipe Promotion, cross-model-family validation is required whenever feasible: the Reviewer arm in the gate uses a model from a different provider or family than the Builder arm, and experiments are run with both same-family and cross-family reviewers to quantify the correlation magnitude. (3) Deterministic gates (build, tests, lint, secret scan, scope checks) are always executed and are never replaced by model-based gates alone; model-based reviewers operate in addition to, not instead of, deterministic checks. (4) Hidden tests (S-04 in security, but also a validity tool) are generated independently of any builder/reviewer model, ideally by human authors or a separate third-party system; hidden-test pass rates are reported separately from same-run model-reviewer pass rates. (5) The Research Charter's Success Criteria (S2, S5) require Pareto improvements over B0/B1 baselines; the baselines use the same reviewer model family, so a shared blind spot hurts them as well, reducing bias in the comparison. |
| **Residual Risk** | **Medium**: Even with different providers, all frontier models share large amounts of common training data, so some residual correlation is unavoidable. The magnitude must be measured via calibration experiments (e.g., injecting known defects into outputs and measuring reviewer detection rates as a function of builder–reviewer pair); this is part of Experiment Family D and should be reported alongside results. |

---

### R-02: Benchmark Contamination

| Field | Description |
|---|---|
| **Threat** | A public benchmark's tasks, solutions, or hidden tests are present in the training data of one or more worker models. The model's measured performance on the benchmark reflects memorization or exposure rather than genuine capability transfer to the task. |
| **Impact** | Spuriously high success rates on contaminated benchmarks; incorrect MCW assignments (a contaminated small model appears "capable" when it is merely memorized); Recipe Promotion backed by inflated evidence; invalid cross-provider comparisons if providers have different training-data overlap with the benchmark. |
| **Mitigation** | (1) Research Plan Tier-1 and Tier-2 tasks are synthetic REDI-Lab-generated products that are not publicly available before experiments run; they cannot be in any model's training data. These are the primary basis for Research Charter hypotheses. (2) External benchmarks (Tier-4) are used for external comparability only, with explicit caveats: every external benchmark report notes the possibility of contamination and reports REDI Lab's results alongside published contamination studies (if available) for that benchmark/model pair. (3) Canary and leakage checks: for any task set, a subset of impossible or trick questions (or uniquely watermarked code snippets) is included; models that have memorized the benchmark answer canary-style on these subsets, and their performance is flagged. (4) Freshness dating: REDI-Lab-generated tasks are dated, and any model released after the task creation date is evaluated with an additional anti-contamination check: the task is modified in a semantically equivalent but syntactically different way, and the result is compared. If performance drops materially, contamination is suspected and the original run is treated skeptically. (5) Recipe Promotion (Experiment Policy Section 19) requires success on held-out REDI-Lab-generated tasks, not only on public benchmarks. |
| **Residual Risk** | **Low–Medium** for REDI-Lab-generated tasks, **Medium–High** for public benchmarks. The risk on public benchmarks is managed by caveats and by using them only as secondary comparators. The fundamental issue is undecidable (we cannot prove a task was not in training data), so mitigation is detection and cautious interpretation. |

---

### R-03: Hidden-Test Leakage

| Field | Description |
|---|---|
| **Threat** | Hidden-test content reaches model context—via a Context Firewall bug, a misconfigured path allowlist, an accidental inclusion in a source file, a bug in the gate execution phase that echoes test output back into a subsequent worker's context, or a deliberate reverse-engineering attempt by a model that guesses hidden tests from visible structure. |
| **Impact** | Inflated pass rates on the primary outcome metric; apparent "quality" where the worker actually cheated or was accidentally told the answer; invalid comparisons between arms where one arm's context accidentally includes more test hints than the other; invalid Recipe Promotion. |
| **Mitigation** | Experiment Policy Section 4 (Hidden-Test Isolation) defines structural separation, leakage-check procedures, canary-token checks, and contamination thresholds. Additional validity mitigations: (1) Hidden tests are split into two partitions per task: a calibration partition used during development and harness debugging only, and a final evaluation partition that is NEVER loaded into any context, including the developer's machine, until formal experiment execution. (2) Gate execution runs in a separate sandbox, reads hidden tests only from Tier-2 storage via a dedicated interface, and never includes test content in the GateResult's model-visible fields. Only pass/fail and non-revealing failure metadata (e.g., "5 of 40 unit tests failed"; specific assertion messages are only included if they do not reveal test intent) are shown to downstream nodes or reviewers. (3) Post-hoc scan: every ContextManifest hash is replayed, and the reconstructed context package is searched for n-gram matches with hidden-test content; matches trigger contamination flags. |
| **Residual Risk** | **Medium**: Structural separation and canary checks catch accidental leakage, but a determined model with good statistical guessing might reconstruct some hidden tests from the visible task structure (especially if the task spec is overly specific). The main defense is that hidden tests are one outcome among many; the Research Charter requires Pareto improvement on multiple metrics, so a treatment that merely inflates hidden-test pass rates without improving cost/tokens/escalations will not succeed overall. |

---

### R-04: Model-Version Drift

| Field | Description |
|---|---|
| **Threat** | Between runs of the same experiment, a provider silently updates its model behind the same alias. Two runs marked with identical WorkerProfile are in fact on different model versions with materially different capability, behavior, or cost. |
| **Impact** | Spurious within-group variance in repeated experiments; false apparent "improvement" or "regression" in a recipe when it's actually the model changing; incorrect Recipe Promotion when a new recipe's runs happen to coincide with a provider-side model upgrade; invalid cross-time comparisons in drift experiments (H10). |
| **Mitigation** | (1) WorkerProfile captures exact model version where available (Experiment Policy Section 7). If the provider does not expose it, the alias is flagged as version-uncontrolled, runs with it carry a confound marker, and drift monitoring runs at elevated frequency. (2) Drift and revalidation procedure in Experiment Policy Section 21: calibration runs are executed on a standard task set periodically; statistically significant deviation from a baseline triggers revalidation. (3) For any longitudinal analysis spanning more than a preregistered time window, runs are either: (a) re-executed on the current version of all worker identities to obtain a current snapshot, or (b) stratified by model-version cohort, so comparisons are within-cohort rather than across-cohort. (4) Important comparisons (Experiment Policy Section 17) prefer models with stable exposed version identifiers where feasible. |
| **Residual Risk** | **Medium–High**: In practice, most providers do not expose stable micro-version identifiers today, and even when they do, they may retire them on short notice. This is a structural property of the hosted-model ecosystem; REDI Lab can detect and account for drift but not prevent it. Longitudinal claims (recipe stability, downshift longevity) must be interpreted with this residual. |

---

### R-05: Provider-Alias Drift

| Field | Description |
|---|---|
| **Threat** | A provider retires an old model name and redirects the alias to a new, cheaper, or differently capable model. A WorkerProfile string-matches the alias but the actual served model is different. This is a more severe variant of R-04 in which the change is not incremental but categorical. |
| **Impact** | Same as R-04, but with larger effect sizes. A full Recipe that relied on the old alias's station-specific evidence could become invalid overnight. If undetected, the optimizer might attribute the performance change to recipe variables and make incorrect promotion/rollback decisions. |
| **Mitigation** | All mitigations from R-04, plus: (1) WorkerProfile includes the provider-echoed model identifier from the response metadata, not just the requested one; discrepancy between requested and echoed is logged and investigated. (2) New-worker onboarding mode (Research Plan Mode D): a provider alias change triggers the new-worker onboarding protocol (shadow runs, historical replay, bounded exploration) instead of silently inheriting the old alias's evidence history. The old alias's evidence is retained but marked "superseded" and is not mixed with the new alias's evidence. (3) Explicit version-pinning contract clauses negotiated with critical providers where possible. |
| **Residual Risk** | **Medium**: Mitigations assume alias changes are detectable, which they are if the provider echoes a real model identifier back. If both the request and response use the same misleading alias, detection must rely on behavioral drift calibration runs, which have statistical detection delay. |

---

### R-06: Environment Drift

| Field | Description |
|---|---|
| **Threat** | Between runs, the software environment changes: dependency upgrades, Python version changes, sandbox runtime version, REDI Lab core version, tool versions, OS patches. Two runs with identical ExperimentManifest, recipe, and worker identities execute in materially different environments. |
| **Impact** | Spurious variance; apparent regression of a recipe that is actually due to a linter becoming stricter or a test framework changing semantics; false Recipe Rollback; invalid comparison between an early arm (run in environment E1) and a later arm (run in E2). |
| **Mitigation** | (1) Experiment Policy Section 6 requires full environment manifests and hashing. Replay pass/fail metrics catch environment-related divergences. (2) Experiments run on pinned environments. Dependency lockfiles are part of the experiment manifest; deviation from the lockfile fails the harness-calibration check. (3) Environment changes are treated as drift triggers per Experiment Policy Section 21.1 and trigger targeted revalidation. (4) Where feasible, experiments run on immutable container images or virtual machine snapshots; the image ID is part of the environment manifest. (5) Analyses of experiments spanning environment changes stratify by environment cohort or include environment as a covariate. |
| **Residual Risk** | **Low–Medium**: Pinned containers and lockfiles handle the vast majority of environment drift. Remote network services fetched during runs (e.g., package registries) remain a vector, mitigated by hash-pinning and offline caching of dependencies. Non-determinism in tests themselves (flaky tests) must be separately quantified via repeated runs. |

---

### R-07: Pricing Drift

| Field | Description |
|---|---|
| **Threat** | Provider pricing changes between runs. A cost comparison between ARM A (run at old prices) and ARM B (run at new prices) uses different unit costs for the same worker identity, producing a spurious cost delta. |
| **Impact** | Invalid downshift decisions (a worker that actually became cheaper is credited to the optimization, or vice versa); incorrect Pareto-frontier plots; invalid H7 and S5 cost comparisons. |
| **Mitigation** | (1) Experiment Policy Section 9 captures pricing_schedule_id per run, with list prices and adjustment recorded separately. (2) All cost analyses can be recomputed under any pricing schedule. Primary analyses use the pricing schedule preregistered in the ExperimentManifest; sensitivity analyses recompute under the contemporary schedule of each arm and under the current schedule, and report all three. (3) Pricing changes are drift triggers; a large-enough pricing change that materially affects the MCW frontier or recipe cost ranking triggers re-evaluation of recipes with the new pricing schedule, though recipe structural validity (quality, topology) is not automatically revoked solely due to pricing change. |
| **Residual Risk** | **Low**: Pricing drift is fully observable and can be corrected analytically post-hoc. The only residual risk is user error: an analysis forgetting to apply the schedule adjustment. This is mitigated by tooling that enforces the recomputation. |

---

### R-08: Caching Effects

| Field | Description |
|---|---|
| **Threat** | Provider-side prompt caching, context reuse, or session-level warm-up effects cause later calls in an experiment to be faster or cheaper than earlier calls, or (in some cases) produce different outputs because of cached internal state. Similarly, local builds in later runs benefit from cached dependencies compared to cold-start runs. |
| **Impact** | Spurious cost/latency improvement between the first and second half of an experiment (order confound); unfair advantage to arms that happen to run after cache-warming; hidden-test pass rate changes if cache-affects context injection; wall-clock-time distortion in parallelism experiments. |
| **Mitigation** | (1) Task order is randomized (Experiment Policy Section 12) so caching affects all arms equally in expectation. Any known systematic caching pattern is either: (a) equalized across arms (force a cache-cold call for every run by using unique request identifiers or session isolation where feasible), or (b) explicitly modeled as a covariate and reported. (2) Wall-clock and aggregate compute are reported separately (Experiment Policy Section 11); caching that reduces only wall-clock via pre-warmed sessions is visible as wall-clock improvement without aggregate-compute improvement. (3) Provider-reported cached-token counts are recorded (Experiment Policy Sections 7 and 10) and analyses run both including and excluding cache-price discounts; the version with the more conservative (higher) cost is used for downshift decisions. (4) Build-environment caches are either cleared between runs (cold-cache condition) or documented as part of the environment and kept consistent across arms; no arm gets a warmer build cache than another. |
| **Residual Risk** | **Medium**: Providers often do not expose fine-grained cache-control knobs, and their internal caching strategies may change without notice. The main defense is randomization (spreads bias evenly across arms) and explicit reporting of any observable cache-related metrics. |

---

### R-09: Human Intervention Bias

| Field | Description |
|---|---|
| **Threat** | Humans intervene in runs: fixing a stuck build, correcting an obvious model typo, adjusting a ProductSpec mid-run, providing hints, or making judgment calls on gate results. Interventions are not applied uniformly across arms—e.g., investigators are more likely to intervene and fix a run in their favored treatment arm than in the baseline arm they expect to fail. |
| **Impact** | Inflated success rates overall; differential inflation favoring the treatment arm; invalid autonomy and quality claims; apparent Recipe improvement that is actually human effort. |
| **Mitigation** | (1) Experiment Policy Section 13: every intervention is timestamped, identity-attributed, diffed, and categorized; non-autonomous runs are flagged; intervention rate thresholds invalidate autonomy claims. (2) Important comparisons (Experiment Policy Section 17) are either: run in fully automated mode where no human intervention is possible and any stuck run simply fails; OR interventions are applied by a blinded operator who does not know which arm is which and follows a preregistered intervention protocol that applies uniformly to all runs. (3) Interventions are never applied inside the model's context or output; they act only on the runtime state (e.g., restarting a crashed harness, not rewriting the builder's source code output); rewriting outputs is a separate, more severe intervention category that invalidates the run for primary quality analyses (the output is no longer the worker's). (4) Per-protocol analyses (excluding intervened runs) and intention-to-treat analyses (including all runs with their final outcomes regardless of intervention) are both reported and compared. |
| **Residual Risk** | **Medium**: Blinding is operationally difficult in small research teams; the smaller the team, the harder it is to separate the roles of experiment designer, operator, and analyst. The primary defense is comprehensive logging and transparent reporting of all interventions so readers can assess the impact themselves. |

---

### R-10: Cherry-Picking

| Field | Description |
|---|---|
| **Threat** | Investigators examine results before finalizing analysis, then select favorable subsets of tasks, runs, time windows, or outcome metrics that show a positive result while discarding the rest. Related: hypothesizing after the results are known (HARKing)—pruning or rewriting hypotheses post-hoc to match observed effects. |
| **Impact** | Inflated Type I error rates; false-positive hypothesis confirmations; spurious Recipe Promotions; misleading publications; reproducibility crisis within REDI Lab when subsequent experiments fail to replicate the cherry-picked effect. |
| **Mitigation** | Experiment Policy Section 16 (Anti-Cherry-Picking) defines: binding preregistered analysis plans, mandatory reporting of all arms/outcomes/task subsets, explicit labeling of exploratory post-hoc analyses, correction for multiple testing, and prohibition on "p < alpha alone" as a promotion criterion. Additionally: (1) Important comparisons (Section 17) require independent sign-off on the manifest before execution. (2) Negative results are retained and reported alongside positive ones (Experiment Policy Section 15); summaries must cite both. (3) Replication runs are built into the promotion ladder (Section 19.6 Shadow → Canary → Validated); effects that survive three distinct rungs with different operator and different task samples are far less likely to be pure cherry-picks. (4) The Evidence Ledger is append-only; runs cannot be retroactively deleted from it. Any analysis that excludes a run must explicitly document the exclusion rule and confirm that the rule was preregistered. |
| **Residual Risk** | **Low–Medium**. Preregistration and append-only ledgers are strong mitigations. Residual risk comes from subtle investigator degrees of freedom in coding, outlier handling, and model specification that preregistration can never fully constrain. The promotion ladder's three independent replication rungs are the main additional defense. |

---

### R-11: Retry-Budget Bias

| Field | Description |
|---|---|
| **Threat** | Treatment and baseline arms have unequal effective retry budgets—either because the manifest sets them unequal, or because a structural difference (e.g., the treatment arm's worker produces failures that, under the retry taxonomy, trigger more retries before hitting budget) gives one arm more attempts per logical node. The arm with more attempts appears more successful simply because it gets more shots at passing. |
| **Impact** | Spurious quality differences favoring the high-retry arm; invalid H3/H4 results if workers are compared with unequal implicit attempts; unfair cost comparisons if the extra attempts' cost is not fully accounted. |
| **Mitigation** | (1) Experiment manifest explicitly sets equal per-node retry budget, per-node total-attempt ceiling, and per-node total-cost ceiling across all arms. Budgets are enforced deterministically by the runtime; the node fails when ANY ceiling is hit. (2) Analyses report both per-attempt success rate (first try only) and per-node eventual success rate (within budget), so the reader can see both. The two are never conflated. (3) Cost and token accounting includes the sum of all attempts (Experiment Policy Section 14). If a treatment arm succeeds only by burning through its entire retry budget, the quality claim is still valid, but the cost claim reflects the actual expense. (4) Failure taxonomy distinguishes retries due to transient environment errors from retries due to capability insufficiency; the two are logged separately, and analyses can condition on them to avoid penalizing an arm simply for experiencing more transient infrastructure failures. |
| **Residual Risk** | **Low**. Retry budgets are a deterministic policy parameter fully under REDI Lab's control. Residual risk is limited to logic bugs in the budget-enforcement code, which are detectable via the ledger's retry record chain and the replay checks. |

---

### R-12: Unequal Context Budgets

| Field | Description |
|---|---|
| **Threat** | Two arms of a worker-comparison or recipe-comparison experiment receive different amounts of input context tokens or different context-selection strategies by accident rather than by design. The arm with more context may appear more capable simply because it sees more information. |
| **Impact** | Spurious confirmation of H2 (context isolation) if the selected-context arm is accidentally given more effective context than the full-repo arm; invalid H3/MCW claims if the smaller-worker arm also gets a larger context budget that compensates for model size; unfair quality comparisons. |
| **Mitigation** | (1) For any experiment where context is NOT the explicit treatment variable, all arms receive equal input-token budgets (or, if budgets differ, the difference is a stated covariate and analysis controls for it). The Context Firewall enforces the budget deterministically. (2) For experiments where context IS the treatment variable (Experiment Family C), arms are explicitly labeled: e.g., "selected context @ 4k tokens" vs "full repo @ 128k tokens." Both per-node success rate AND success per input token are reported, so the tradeoff is visible. The experiment's hypothesis may be about efficiency per token, not about raw success at any cost. (3) Every RunEvidence record contains the ContextManifest hash and total tokens (Experiment Policy Section 8), and the post-experiment audit verifies that per-arm average total input tokens match the manifest's design within tolerance. Mismatches cause the affected runs to be flagged. |
| **Residual Risk** | **Low–Medium**. Budget enforcement is deterministic. Residual risk comes from the "effective context" problem: two context packages with the same token count can vary wildly in how much relevant information they contain. This is exactly what H2 and the context-isolation experiments are designed to measure; as long as token counts are equal, the experiment is fair. The risk is unequal token budgets due to a bug, which the audit catches. |

---

### R-13: Unequal Tool Access

| Field | Description |
|---|---|
| **Threat** | Two arms in a worker-comparison or recipe-comparison experiment have different sets of available tools: e.g., the treatment arm's worker gets a linter tool or a multi-edit tool that the baseline arm does not, leading to better outcomes that are due to tools, not worker or recipe merit. |
| **Impact** | Spurious confirmation of H1, H3, or H6; invalid downshift decisions; a recipe that looks good only because of a tool advantage. |
| **Mitigation** | (1) Baseline execution parity (Experiment Policy Section 3.2) requires identical tool permissions across arms unless the tool is explicitly the treatment variable. (2) StationContracts define the tool allowlist; WorkerPool members for the same station all run under the same StationContract with the same tool set. (3) Experiments comparing different stations or different DAG topologies (Experiment Family I, H8) explicitly note tool-difference as part of the treatment; the hypothesis is precisely about the station/tool combination, so "different tools" is not a confound—it's the intervention. In that case, however, any claim about "worker capability" must be reworded as a claim about the station+worker+tool configuration; the conclusion is about the combined production unit. |
| **Residual Risk** | **Low** for within-station worker comparisons (same contract → same tools). **Medium** for cross-topology or cross-station comparisons, where the intervention inherently changes tools and stations—and the conclusion must be phrased carefully to match. The main residual risk is misphrasing a result (claiming "worker X is better" when actually "station+tool Y + worker X is better"). |

---

### R-14: Misleading Parallel Latency Measurements

| Field | Description |
|---|---|
| **Threat** | A treatment arm exploits more parallelism than the baseline arm, producing lower wall-clock time but equal or higher aggregate compute. Reports cite only the wall-clock improvement and present it as an efficiency gain, hiding the fact that more total resources were consumed. Worse: the treatment arm is given more execution capacity (more parallel workers) than the baseline, making the "parallelism advantage" an artifact of unequal hardware allocation rather than of the DAG topology. |
| **Impact** | Spurious wall-clock superiority claims; invalid H8 or S5 results; cost/latency Pareto misrepresented if aggregate cost (not just wall-clock) actually worsened. |
| **Mitigation** | (1) Experiment Policy Section 11 mandates reporting both wall-clock and aggregate worker compute + gate compute + scheduler idle separately for every FactoryRun. Wall-clock-only claims are prohibited; any mention of speed must be accompanied by aggregate compute so the reader can see the tradeoff. (2) For parallelism comparisons (Experiment Family I, H8), execution capacity is either: equalized across arms (e.g., both run on a pool of N workers, so the more-parallel DAG's advantage is real under the same hardware budget), OR the capacity difference is explicitly labeled and both scenarios are measured. The default is equal capacity. (3) Scheduler idle seconds are measured, so an arm that wall-clock-wins only because it threw more capacity at the problem is visible via higher idle time in the baseline (if the baseline's DAG cannot utilize the capacity). The idle data is reported. |
| **Residual Risk** | **Low–Medium** if both metrics are always reported; readers can interpret the tradeoff correctly. Residual risk is limited to misleading communication by humans, which the mandatory reporting rules and Research Charter Section 11 claim discipline mitigate. |

---

## 3. Threat-Mitigation Coverage Summary

The table below maps threats to the REDI Lab policy and architecture documents whose controls provide primary mitigation.

| Threat ID | Primary Mitigation Document(s) |
|---|---|
| S-01 Prompt Injection | Architecture (Context Firewall, Station Contracts, Gate Engine); Experiment Policy (Harness Calibration) |
| S-02 Malicious Dependencies | Architecture (Sandbox Abstraction, Control-Plane Isolation, Deterministic Gates) |
| S-03 Secret Leakage | Data Policy (D11 rules, Section 6); Architecture (Event Router, Runtime Adapter) |
| S-04 Destructive Tools | Architecture (Station Contracts, Sandbox Abstraction); Data Policy (Tiered Storage) |
| S-05 Filesystem Escape | Architecture (Sandbox Abstraction, Handoff Manifest, Workspace Isolation) |
| S-06 Network Abuse | Architecture (Sandbox Network Policies, Event Router Scoping) |
| S-07 Privilege Escalation | Architecture (Security Domains, Event Router Validation, RBAC) |
| S-08 Unauthorized Scope Changes | Architecture (Versioned Contracts, Scope Change Approval) |
| S-09 Unsafe Parallel Workers | Architecture (Scheduler, DAG, Handoff Manifest, Integration Gates) |
| S-10 Cross-Workspace Contamination | Data Policy (Tiered Storage, Recipe Scope Checks); Architecture (Workspace Isolation) |
| S-11 Malicious Generated Code | Architecture (Deterministic + Model Gates, Security Station); Research Charter (Non-goals, Claim Discipline) |
| S-12 Compromised Providers | Architecture (Provider-Independent Adapters, Cross-Provider Validation); Experiment Policy (Drift) |
| R-01 Evaluator Correlation | Research Charter (Baselines); Experiment Policy (Gate Parity, Deterministic First) |
| R-02 Benchmark Contamination | Research Plan (Tiered Tasks); Experiment Policy (Task Set Provenance) |
| R-03 Hidden-Test Leakage | Experiment Policy (Section 4, Hidden-Test Isolation) |
| R-04 Model-Version Drift | Experiment Policy (Section 21, Drift/Revalidation) |
| R-05 Provider-Alias Drift | Experiment Policy (Section 7, Worker Identity; Section 21, New-Worker Onboarding) |
| R-06 Environment Drift | Experiment Policy (Section 6, Environment Capture; Section 5, Replay) |
| R-07 Pricing Drift | Experiment Policy (Section 9, Pricing Schedule Versioning) |
| R-08 Caching Effects | Experiment Policy (Section 12, Randomization; Section 11, Dual Timing) |
| R-09 Human Intervention Bias | Experiment Policy (Section 13, Intervention Logging; Section 17, Sign-Off) |
| R-10 Cherry-Picking / HARKing | Experiment Policy (Section 16, Anti-Cherry-Picking; Section 15, Negative Results) |
| R-11 Retry-Budget Bias | Experiment Policy (Section 14, Retry Logging; Section 3, Parity) |
| R-12 Unequal Context Budgets | Experiment Policy (Section 8, Context Manifest Audit; Section 3, Parity) |
| R-13 Unequal Tool Access | Experiment Policy (Section 3, Parity); Architecture (Station Contracts) |
| R-14 Parallel Latency Mislead | Experiment Policy (Section 11, Dual Timing Reporting) |

---

## 4. Change Control

This Threat Model is frozen at REDI-00A with version 1.0.

New threats, changes to impact ratings, or modifications to mitigation strategies require:
1. A documented rationale citing a specific incident, research result, new attack, or architecture change.
2. The change is reviewed for consistency with the Research Charter, Experiment Policy, Data Policy, and Architecture Document.
3. Version increment and dated changelog entry appended to the end of this document.
4. If the change affects the validity controls of an ongoing experiment, the experiment's manifest is either frozen as-is (the old threat model governs it) and the change is noted as a limitation, or the experiment is re-baselined under the new version (with appropriate documentation of the change).
