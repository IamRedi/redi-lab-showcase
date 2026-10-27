# REDI-10B: stable-baseline qualification (offline governance freeze)

Status: offline criteria/manifest/lifecycle freeze. No provider calls, no real
promotion, no Recipe Store mutation, no REDI-10C/REDI-11 work.

## What this adds

`redi_lab/stable_baseline.py` defines the versioned, hash-frozen contract a
real recipe must satisfy BEFORE it can be called VALIDATED/STABLE and before
any live continual-optimization experiment may run. It is a governance layer
above `RecipeStore` (which deliberately blocks `promote_to_validated`); the
store is only ever read.

## Predeclared V0 stability criteria (version 1, hash-frozen)

| Criterion | V0 floor | Justification (preregistered) |
| --- | --- | --- |
| Independent product coverage | >= 3 distinct products | the store's own confidence model marks single-product evidence LOW/SINGLE_PRODUCT_EVIDENCE; 3 is the smallest set with cross-product majority agreement |
| Verified transfers | >= 3 (one per product) | fewer cannot support a stability claim |
| Quality non-regression | all products | paired verified quality vs baseline on the same inputs |
| Reproducibility/replay | replay x2 identical per product, from persisted bytes only | REDI-07..10 replay discipline |
| Mandatory gates | private, integration, reviewer, scope/security all PASS | identical to the frozen MCW/transfer gate set |
| Rollback readiness | exact immutable prior version whose own quality evidence is VERIFIED | REDI-08 finding: v6's lineage target v5 failed its holdout, so the rollback path is not quality-proven |
| Evidence completeness | hash-chained provenance reconciled into governed records | unreconciled transfer results are not auditable evidence |
| Validity window | bounded, <= 90 days | unbounded stability claims are forbidden by the research charter |
| Stability dwell | >= 14 days at VALIDATED with zero recorded regression before STABLE | minimum observation window |
| Human/audit approval | explicit approval citing the exact evidence hash at BOTH rungs | REDI-08/09 governance |

The thresholds were anchored to governance principles (store confidence
semantics, replication logic, replay discipline) and recorded before any
qualification outcome; changing any of them requires a NEW criteria version.

## Lifecycle (no silent promotion)

```
SHADOW --criteria met--> ELIGIBLE_FOR_VALIDATION --approval--> VALIDATED
        |                                                     |
        +--criteria unmet--> VALIDATION_BLOCKED (stays SHADOW) +--dwell >= 14d, approval--> STABLE
        +--regression-------> VALIDATION_REJECTED              +--regression--> ROLLED_BACK
                                                                                    (exact immutable target)
```

- `evaluate()` never promotes: unmet criteria append a `VALIDATION_BLOCKED`
  event with the exact failing checks and the state stays SHADOW.
- `approve_validated` / `approve_stable` require a `QualificationApproval`
  citing the exact expected evidence hash (frozen manifest identity for
  VALIDATED; the recorded dwell evidence hash for STABLE).
- `rollback` requires approval pinning the rollback target's canonical hash.
- The ledger is append-only and hash-chained; replay reconstructs state from
  persisted bytes alone (x2 identical under current-policy drift) and detects
  any tampering.

## Current v6 eligibility (read-only audit)

`v6_qualification_audit()` reads the real REDI-08c14 store read-only and
judges `recipe_cold_39553c39ceb593e2bc24775be1c420e2` v6 against the frozen
criteria. Result: **NOT ELIGIBLE**. Existing REDI-07/08/09 evidence already
satisfies only these subsets: one verified transfer with replay x2 and
quality non-regression on a single holdout product. Exactly missing:

1. `INDEPENDENT_PRODUCT_COVERAGE`: 1 of >= 3 distinct products (in-store
   sibling products = 0; the verified transfer covers one product).
2. `VERIFIED_TRANSFERS` / `MANDATORY_GATES` / `REPLAY_X2_PER_PRODUCT` /
   `QUALITY_NON_REGRESSION` / `EVIDENCE_COMPLETENESS`: each requires >= 3
   complete per-product evidence records; today there is 1 verified transfer
   and its provenance was never reconciled into the store's governed records.
3. `ROLLBACK_TARGET_QUALITY_EVIDENCE`: v6's lineage rollback target (v5)
   failed its holdout, so the rollback path has no verified quality proof.
4. Not yet applicable but required later: bounded validity-window
   registration and explicit human/audit approvals at both rungs.

Minimum additional independent evidence for v6 to qualify (§8): >= 2 further
distinct-product verified paired holdout transfers with full gate evidence,
replay x2 proof, reconciled provenance, a quality-verified rollback target
(re-qualify v5 or pin a different verified version), a registered validity
window, and owner approvals at both rungs. v6 is NOT promoted by REDI-10B.

## Dry proof

The offline tests prove all five required behaviours: insufficient evidence
blocks validation (state unchanged, gaps recorded); sufficient evidence
yields ELIGIBLE_FOR_VALIDATION only; explicit approval is required for both
VALIDATED and STABLE (wrong evidence hash refused); regression rejects
before validation and rolls back after STABLE to the exact immutable target;
replay x2 is deterministic, tamper-evident, provider/network-free, and leaves
the real Recipe Store byte-identical.
