# Autonomous Recovery & Escalation v1

New Professional Product Factory runs use `autonomous_recovery_v1`. Existing evidence, legacy pharmacy/single-call/CRM execution contracts and historical FAILED outcomes are not rewritten. Worker failure is an internal event; owner terminal outcomes are APP READY or OWNER ACTION REQUIRED. APP READY still requires every frozen gate, packaging, real-browser acceptance and Medaurus review.

## Decision and evidence contracts

Bato is a deterministic first responder, not an extra paid model call. `recovery_diagnosis_v1` records the failure class, station, artifact, exact cause, confidence, local-fix eligibility, correction suitability, contract/replan needs, escalation recommendation, impacted descendants, owner requirement and evidence references. Evidence contains the immutable ProductSpec, task contract, retained output, producer interfaces, expected checks and earlier attempts. Recovery context is bounded; overflow requires explicit review rather than silent truncation.

Zojz uses `recovery_decision_v1`: action, reason, authorized execution profile, counters and configured limits. Decisions are persisted before execution. Actions are LOCAL_REPAIR, SAME_STATION_CORRECTION, CONTRACT_REPAIR_AND_REPLAY, PARTIAL_REPLAN, CAPABILITY_ESCALATION, PROVIDER_RETRY and OWNER_ACTION_REQUIRED.

Failure classes are DETERMINISTIC_LOCAL_DEFECT, WORKER_IMPLEMENTATION_DEFECT, CONTRACT_MISMATCH, PLANNING_DEFECT, CAPABILITY_LIMIT, PROVIDER_TRANSIENT, PROVIDER_HARD_BLOCKER, OWNER_DECISION_REQUIRED, EXTERNAL_DEPENDENCY_BLOCKER and HARNESS_OR_GATE_DEFECT. Known provider status/code takes precedence over implementation diagnostics. Unknown provider errors fail closed. An evaluator failure is never permission to weaken its assertion or mutate the gate.

Each `recovery/episode-NNNN/` retains evidence, diagnosis, decision, an immutable start record and final `recovery_episode_v1`. Episodes include original/corrected hashes, profiles, checks, outcome, call-derived cost/token deltas, elapsed time and branch invalidations. Provider attempts and failed artifacts get separate paths; original attempts are never overwritten. No hidden chain-of-thought is requested or stored.

## Boundaries

The only v1 local artifact normalization is `json_envelope_bom_v1`: remove a leading Unicode byte-order mark from the JSON envelope, then parse again. It does not alter Python source or business behavior. A null aggregate is corrected by the responsible station, not silently patched by the factory.

One targeted same-station correction is permitted by default. The prompt includes original contracts, actual failure, expected checks, failed bytes and prior attempts. The complete station integrity suite reruns, including real Chromium for Teuta. VERIFIED requires those checks to pass.

Contract repair and partial replanning use the existing deterministic Ora planner against the same immutable ProductSpec. The planner regenerates the authoritative interfaces/dependencies, calculates changed tasks and their transitive descendants, and retains completed independent tasks. It persists both plans, invalidated/retained sets and unchanged acceptance criteria. This v1 can repair drift from the supported ProductSpec-derived plan; it cannot invent unsupported product capabilities or silently change the specification.

## Configuration and spend

`RecoveryBudget` is an explicit runner configuration: one local repair, one same-station correction, one escalation, one contract repair, one partial replan and one transient retry per station; maximum 20 provider calls and 1,200 seconds per run; one-second retry backoff. Limits can be lowered or raised through runner configuration. The existing explicit owner monetary ceiling remains binding across initial and recovery calls. Failed calls with uncertain billing retain their conservative cost reservation. Episodes mark cost/token completeness and retain unresolved reservation amounts. Truncated responses preserve safe partial output and classify as CAPABILITY_LIMIT, permitting bounded escalation rather than an infrastructure retry.

`RECOVERY_HIGH_CAPABILITY` separates persona from execution profile. The governed adapter defaults to the already configured production model with high reasoning and a larger output allowance. This is a capability-profile escalation, not a claim that a different model is superior. `REDI_RECOVERY_PROFILES` can supply a JSON array of explicitly authorized profiles (`id`, `authorized`, `model`, `capability_rank`, `reasoning_effort`, `output_cap`, `context_budget`). Zojz selects the highest configured rank above the initial profile. The provider adapter must support and price that model; unknown models fail before a paid call. An empty list disables escalation and leads to an explicit owner action when a stronger profile is needed. Profile names never change station identity or artifact ownership.

Transient retries require recognized timeout/connection failures, temporary 5xx, or an explicitly retryable rate-limit code. Invalid credentials, insufficient credits, disabled/restricted accounts and permission errors do not retry. An unknown 429 is not assumed transient. SDK automatic retries remain disabled.

OWNER ACTION REQUIRED records one of CREDENTIAL_REQUIRED, CREDITS_REQUIRED, SPEC_DECISION_REQUIRED, EXTERNAL_SERVICE_REQUIRED, UNSUPPORTED_CAPABILITY, RECOVERY_EXHAUSTED, SAFETY_OR_POLICY_BLOCKER or MANUAL_APPROVAL_REQUIRED. It includes the station, attempted repairs, stronger-profile use, evidence and concrete decision needed. Nonempty ProductSpec uncertainties are conservatively returned to the owner; no consequential ambiguity is silently resolved.

## Learning and UI

Only successful recovery episodes in an APP READY run produce `recovery_lesson_candidate_v1`. Scope includes product/task class, storage and field types. Candidates carry evidence, confidence, proposed change, expected benefit, risk, rollback and repeated-evidence requirement. Nothing promotes policy automatically.

Workbench keeps the run active during RECOVERY, REPLANNING, ESCALATED and VERIFYING. Bato ANALYZING and Zojz RECOVERY DECISION come from real events. Reverified tasks supersede earlier internal failure in current-state presentation while original events remain retained. Old historical failures keep their original meanings. Product identity continues to follow the canonical ProductSpec/owner-name repair.

## Offline acceptance

`tests/unit/test_autonomous_recovery.py` covers SQL SUM(NULL), malformed UI, failed targeted correction then configured-profile escalation, contract drift, cyclic planning, retained PulseBudget bytes, credits, ambiguity, transient exhaustion, global call limits, governed profile accounting, deterministic envelope repair and UI state mapping. Successful scenarios use the real integrity and frozen gate infrastructure, real SQLite and Chromium; injected profiles prove routing, not model superiority. No live PulseBudget build is part of this milestone.
