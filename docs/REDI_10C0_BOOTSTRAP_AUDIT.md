# REDI-10C0: first-stable-baseline bootstrap audit (offline)

Question audited: does the REDI-10B rule "rollback target must have its own
verified quality evidence" make qualification of the FIRST-EVER stable recipe
circular or impossible?

## Verdict: bootstrap circularity CONFIRMED (for the canonical first baseline)

Under criteria v1, the rollback target must be a distinct immutable recipe
version carrying its own verified quality evidence.  For the first-ever
stable baseline the canonical pre-promotion operational state is "no promoted
recipe" (the fallback-worker path).  That state is not a recipe and cannot
carry recipe-level verified quality evidence.  In REDI's actual history every
pre-v6 recipe version failed its holdout, so no prior version can serve
either.  Under v1 the first baseline is therefore blocked indefinitely, or
actors are incentivized to fabricate a no-op "rollback recipe" — gaming the
lifecycle.  Note precisely: v1 is not logically unsatisfiable in all worlds
(a prior experimental version with independently verified evidence could
serve), but it is defective for the canonical bootstrap case, and no legitimate
prior state exists in this project.

Case distinction now encoded:

- A. First-ever stable baseline (no STABLE recipe exists in scope):
  rollback = restore the EXACT previous operational state.
- B. Later promotions: rollback = the v1 rule (immutable quality-verified
  prior stable recipe), unchanged.

## Criteria v2 (fixes ONLY the bootstrap rule; v1 immutable)

`StableBaselineCriteriaV2` (version 2) inherits every v1 floor unchanged and
adds, for first-baseline qualifications only, the alternative rollback path.
All three are mandatory and cannot be weakened:

1. `BootstrapPriorState`: the exact previous operational state captured and
   hash-pinned (`captured_state_hash`), with a fallback description.
2. Deterministic restoration proof: `restoration_procedure_hash` plus
   `restoration_replay_x2_identical` (replay x2 from persisted bytes).
3. Explicit owner approval of the capture (`owner_approved_by` +
   `owner_audit_reference`).

Governor enforcement: a bootstrap qualification REQUIRES criteria v2
(constructing a bootstrap governor under v1 raises — no silent weakening);
rollback approval in bootstrap mode must pin the exact captured prior-state
hash and the rollback evidence records restoration of that state; a
non-bootstrap v2 qualification still requires pinned quality evidence for the
recipe rollback target (now checked inside `evaluate_criteria` as
`ROLLBACK_TARGET_QUALITY_EVIDENCE`).  `evaluate_criteria` keeps v1 semantics
byte-identical for v1 criteria (the v1 hash `47086bbf…` is untouched).

## v6 re-evaluation (read-only; NOT promoted)

Against the real REDI-08c14 store:

- v1: NOT ELIGIBLE — the six product/transfer criteria fail (1 of >=3
  products, 1 incomplete evidence record) plus
  `ROLLBACK_TARGET_QUALITY_EVIDENCE` (v5 failed).
- v2: NOT ELIGIBLE — the same six criteria fail unchanged; the bootstrap
  rollback path is now open in principle but no captured, owner-approved
  prior operational state with a restoration replay proof exists, so
  `BOOTSTRAP_ROLLBACK_PATH` also fails.  v2 removes the circularity, not the
  evidence burden.

## Remaining evidence gaps for v6 after the bootstrap fix

1. >= 2 further distinct-product verified paired holdout transfers with all
   mandatory gates, quality non-regression, and replay x2 (1 of 3 exists).
2. Reconciled, hash-chained provenance for all per-product evidence
   (`EVIDENCE_COMPLETENESS`).
3. A captured, owner-approved prior operational state record with a
   deterministic restoration replay proof (`BOOTSTRAP_ROLLBACK_PATH`).
4. Registered bounded validity window and explicit owner approvals at the
   VALIDATED and STABLE rungs (downstream requirements).

Zero provider calls, zero promotion, zero store mutation (asserted by test).
