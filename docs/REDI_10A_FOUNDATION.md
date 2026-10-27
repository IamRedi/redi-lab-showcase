# REDI-10A: stable-baseline audit and offline optimization foundation

Status: offline foundation. No provider calls, live optimization, Recipe Store promotion, or production recipe changes are authorized by this document.

## Starting evidence and baseline verdict

Starting HEAD was `b0af5e02364d8af6ca207a9ef909b9ba03d714c3`. The accepted REDI-07/08/09 closures are preserved. REDI-09's N8 result is scoped to `station_mcw_implementation_v0` / `mcw_python_component_v1`: SMALL 6/8, MID 8/8 `CANDIDATE_SUFFICIENT`, FRONTIER 7/8. These counts are a candidate prior for that stratum only.

The tracked `.gitattributes` entry preserves the REDI-09E runner's accepted LF byte sequence across Windows checkouts. It changes no runner logic or historical binding; without this, `core.autocrlf=true` changed its raw SHA-256 in a new worktree and the existing fail-closed runner tests refused to start.

The latest owner REDI-08 transfer store inspected read-only is `build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite` in the existing REDI Lab workspace. Recipe ID `recipe_cold_39553c39ceb593e2bc24775be1c420e2` has these immutable versions:

| Version | Lifecycle | Parent | Rollback target |
| --- | --- | --- | --- |
| 1 | candidate | none | none |
| 2 | shadow | 1 | 1 |
| 3 | shadow | 2 | 2 |
| 4 | shadow | 3 | 3 |
| 5 | shadow | 4 | 4 |
| 6 | shadow | 5 | 5 |

Version 6 is selected for controlled transfer, not production. Its Recipe Store content hash is `de3aec903f7cf43fee3cf09f65503b8a82c2b81727cdcfe5515926af75dd8e3e`; the transfer result separately records semantic recipe hash `8577fa3b0b81c51d368940f1ec74a62050de7723351264efdaa96057d8931daf`. The latest store has no `VALIDATED` version or production-promotion event. Its embedded confidence remains `LOW`, `SINGLE_PRODUCT_EVIDENCE`, source verified runs 1, distinct sibling products 0, transfer evidence count 0; the subsequent verified transfer result did not rewrite the stored recipe metadata.

The owner holdout results for versions 4, 5 and 6 are respectively failed, failed, and first failed then verified (`redi08c6`, `redi08c8`, `redi08c10`, `redi08c12`, `redi08c14` owner runs). The last `redi08c14` result says `verified=true`, `counts_as_transfer_evidence=true`, `recipe_promoted=false`, and explicitly declines a stability claim. Its selected implementation worker is `worker_openai_gpt56_terra_holdout_v0` version 3, model `gpt-5.6-terra`, Chat Completions, strict JSON schema, `max_completion_tokens=2048`, `reasoning_effort=low`; the independent reviewer remains in the stored worker preferences. The transfer replay declares zero provider calls, zero network, no current worker-registry influence and no recipe reselection. The immutable v6 rollback lineage points to v5; that is not a validated production rollback path.

**REDI-10A stable-baseline verdict: `STABLE_BASELINE_NOT_YET_ESTABLISHED`.** `PRODUCTION_OPTIMIZATION_BLOCKED_UNTIL_STABLE_BASELINE` is binding. A verified single transfer and an MCW candidate result do not establish a stable production recipe.

## Promotion governance audit

`docs/experiment-policy.md` §§18–21 requires preregistered evidence quantity/coverage, quality non-inferiority, replay and leakage audit, human sign-off, gradual Candidate → Shadow → Canary → Validated promotion, and exact rollback. `docs/REDI_LAB_RESEARCH_PLAN.md` specifies rule-based human/audit-approved V0 promotion. `redi_lab/recipe_store.py` deliberately blocks `promote_to_validated`; it has only controlled Shadow enablement and append-only selection/rollback events.

**GOVERNANCE GAP:** no approved pilot threshold package fixes the minimum independent product count, field-coverage floor, quality margin, gate bounds, power/repetition plan, rung duration, or post-promotion regression window for this recipe. No current validated baseline exists for a production downshift. REDI-10A does not choose a favorable threshold from observed results. The synthetic test rule (one pair, exact gate equality, cheaper cost) is an engineering fixture only, not a production scientific acceptance rule.

## Offline contract and controls

`redi_lab/continual_optimization.py` defines a versioned, hash-frozen `OptimizationExperiment`: baseline and candidate recipe ID/version/hash, one material treatment delta, exact station/task class, exploration budget, evidence and regression gates, pre-result promotion rule, exact rollback target, revalidation policy, and bounded validity window. REDI-10A accepts `synthetic_only=True` only. It has no adapter that can update `RecipeStore` or select a production recipe.

The deterministic fixture budget is at most 2 experiments, 0 provider calls, 0 monetary spend, 2 failed candidates and 60 seconds. Counters are cumulative. Exhaustion appends evidence and stops; no retries occur. A future live budget would require a separate approved versioned manifest and implementation.

The scoped MCW prior pins REDI-09D expansion manifest hash `c7d5606c340911fcbf5f2a233ac968de3768b1ef5eae13f108c9c23bc4bfa335` and derived N8 aggregation hash `04cde5b229eaff0641f8f62c317642a8e80e5cdd0d519161917f52141e67a5f0` (canonical JSON SHA-256 of the accepted locked N5 + valid expansion + REDI-09F v2 rerun aggregation). Its accepted REDI-09F freeze hash is `3aa07cfca0fb37f39b8af091121b54b8de0da7a62842aa321038fac3e0c4be9b`. The only positive candidate scope is `station_mcw_implementation_v0` / `mcw_python_component_v1`; this is neither a global model ranking nor a promotion decision. The N8 result retains small-sample and pricing-evidence uncertainty.

Shadow evaluation runs both frozen fake workers on the same task hash. It records paired verified quality, each mandatory gate, usage, end-to-end cost, latency, failures, context hash and worker identity/version. The candidate has no output authority. A separate append-only, hash-chained JSONL ledger records manifest, budget, pairs, lifecycle decisions, drift and revalidation. Replay reconstructs from stored bytes alone, twice identically under current-policy drift, without provider/network/registry access.

The dry lifecycle is `CANDIDATE → SHADOW → ELIGIBLE_FOR_PROMOTION → PROMOTED`, with `REJECTED` for preregistered regressions and `ROLLED_BACK` for a promoted candidate that later fails. Promotion requires exact paired-evidence hash plus explicit human/audit approval. The post-promotion dry check applies the frozen quality/security gates and, on regression, returns to the exact immutable prior recipe hash under preauthorized rollback. This is a simulation state; it does not mark any Recipe Store version Validated.

Versioned descriptive drift observations cover task distribution, model/provider version, context policy, gate behavior, verified quality, cost and latency. Numeric drift requires sufficient samples in two consecutive windows; an isolated variation is not drift. A drift signal records `TARGETED_REVALIDATION_REQUIRED` with only the affected station/task/worker, required evidence and an explicit retain/rollback-required/revise-candidate decision. It cannot mutate recipe selection. A fake new WorkerProfile follows the same candidate-pool → historical-replay → shadow → bounded-exploration → eligibility path without a new station or provider-specific recipe structure.

Metrics remain a vector: successful downshift rate, quality non-regression, cost delta, latency delta, worker-selection regret, recipe revision frequency, drift detection delay and rollback rate. Shadow evidence alone cannot estimate selection regret, so it is labeled unavailable; no composite winner score is produced.

## Dry proof and next gate

The offline tests cover: cheaper/equal-quality candidate eligible but not auto-promoted; cheaper/regressed candidate rejected; explicit approval and later regression rolling back to the prior immutable version; sustained drift and targeted-only revalidation; new fake worker onboarding; budget stop; no silent learning; tamper detection; replay twice; and zero network/provider calls. `scripts/run_redi_10_validations.py` runs the REDI-10 pack and can run the full unit suite plus REDI-01..09 validation packs with `--all`.

Before the first live optimization experiment: approve a pilot-derived threshold package and promotion protocol **before results**, establish an actual Validated production baseline with independent product coverage, reconcile transfer evidence into governed provenance without mutating historical recipe bytes, verify replay/rollback for that baseline, preregister a scoped live experiment and cost/call budget, obtain explicit owner authorization, then design REDI-10B separately. None of those steps is performed by REDI-10A.
