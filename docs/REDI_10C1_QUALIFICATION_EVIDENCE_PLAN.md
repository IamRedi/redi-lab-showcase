# REDI-10C1: v6 stable-baseline qualification evidence plan (offline freeze)

Status: evidence-acquisition plan frozen. No provider calls, no promotion, no
store mutation, no REDI-11. Live execution requires separate owner authorization.

## 1. First-baseline rollback state (captured; no rollback recipe fabricated)

`capture_prior_operational_state()` reads the real store read-only and freezes
the legitimate pre-promotion state: no VALIDATED/selected recipe for
`station_cold_start_implementation_v0`; the operational path is the accepted
REDI-07B fallback profile `worker_cold_openai_live_fallback_v0`
(gpt-4o-mini-2024-07-18, policy `redi07b_one_live_fallback_v0`), with the
independent reviewer (`worker_pair_independent_reviewer_v0`) executed by the
local deterministic executor (non-provider). Persisted: definition, config
hash, 3-step restoration procedure + hash, and a read-only restoration
rehearsal run twice with identical results. `captured_state_hash` is pinned in
the manifest. No rollback recipe is fabricated anywhere.

## 2-3. Independent-product evidence plan (neutral, deterministic)

A 4-candidate frame of NEW same-class products (`stationery_supply`,
`pharmacy_supply`, `hardware_restock`, `pantry_restock`; class
`inventory_replenishment_utility`, signature hash `96da7671…`). Seed =
SHA256(signature hash + v6 canonical hash + `"REDI-10C1-evidence-v1"`) —
pre-result identifiers only; no worker failures, v6 behavior, difficulty, or
pass-likelihood enters. Ranks select exactly 2: **`stationery_supply`** and
**`pharmacy_supply`** (deterministic, reproducible). Class-compatibility proofs
(all three interface categories, stdlib family, signature policy) and
distinctness proofs (vs `inventory_math` source, `depot_replenishment` holdout,
existing markers, each other) are mechanized.

## 4-6. Paired comparisons and frozen decision rule

Per product, a `PairedComparisonSpec`: arm A = prior operational baseline
(fallback worker, 1 provider call), arm B = v6 warm-start
(`worker_openai_gpt56_terra_holdout_v0` v3, gpt-5.6-terra, reasoning low,
1 provider call). Shared and frozen: fixture, context, mandatory gates
(private/integration/reviewer/scope-security), one-call-per-arm, no retry, no
fallback. Quality non-regression is decided ONLY from the deterministic gates;
no subjective score, no weighted metric, no post-result criterion change.
Required per-product evidence: baseline execution, v6 execution, all gates,
verified transfer result, non-regression decision, replay x2, hash-chained
provenance.

## 7. QualificationEvidenceManifest (frozen before live execution)

`build_evidence_manifest()` binds criteria v2 hash, v6 identity
(`recipe_cold_39553c39ceb593e2bc24775be1c420e2` v6, canonical hash `de3aec90…`),
the existing depot transfer evidence (single verified product), the two
selected ProductSpecs, the rollback captured-state hash, the exact execution
order, the quality criterion, replay-x2 and no-retry policies, the 90-day
validity-window policy, and APPROVAL SLOTS ONLY (`{"validated": None,
"stable": None}` — never a pre-approval).

## 8. Approval semantics

ELIGIBLE_FOR_VALIDATION may be reached by evidence alone; VALIDATED still
requires explicit owner/audit approval citing the exact evidence hash; STABLE
additionally requires the frozen 14-day regression-free dwell.

## 9. Future live requirements

- Provider calls: **4** (2 products x 2 arms x 1 call; reviewer = 0 calls)
- Models/workers: gpt-4o-mini-2024-07-18 (fallback arm),
  gpt-5.6-terra (v6 arm), deterministic reviewer (no calls)
- Expected cost: **USD 0.0217088**; hard configured bound: **USD 0.060416**
  (pinned schedule `openai_standard_text_pricing_2026_09_22_v1`)
- Owner ceiling: NOT chosen here; must cover the hard bound.

## 10. Dry proof

Fake-provider rehearsal: post-acquisition bundle (depot provenance reconciled
per acquisition step 3 — historical store bytes untouched) evaluates
ELIGIBLE_FOR_VALIDATION under criteria v2 with the bootstrap rollback path;
approval is still required; replay x2 deterministic and network-blocked. One
failed product blocks qualification (stays SHADOW, gaps recorded). The
current-state bundle honestly fails `EVIDENCE_COMPLETENESS` (depot provenance
unreconciled), proving the gap is real and the plan addresses it.
