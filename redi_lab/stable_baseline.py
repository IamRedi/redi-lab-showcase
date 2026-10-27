"""REDI-10B — stable-baseline qualification control plane (offline only).

Defines the predeclared evidence/governance requirements a real recipe must
satisfy BEFORE it can become VALIDATED/STABLE and before any live continual-
optimization experiment is allowed.  This is a governance layer ABOVE
``RecipeStore``: it never mutates the store, never promotes anything real,
and can never execute a provider call (like REDI-10A, ``synthetic_only``).

The V0 criteria below are predeclared floors for a *stability* claim.  They
are anchored to governance principles already encoded in the store's own
confidence model (single-product evidence is LOW confidence) and to the
promotion-governance gap list recorded in REDI-10A — not tuned to any one
recipe's known results.  Changing any threshold requires a NEW criteria
version; historical qualifications keep the version they were judged under.

Lifecycle (explicit human approval at both promotion rungs; no silent path):

    SHADOW -> ELIGIBLE_FOR_VALIDATION -> VALIDATED -> STABLE
                (criteria met)          (approval)  (approval + dwell)

    VALIDATION_BLOCKED  (criteria unmet; stays SHADOW, gaps recorded)
    VALIDATION_REJECTED (regression during qualification evidence)
    ROLLED_BACK         (regression after STABLE; exact immutable target)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .domain import RecipeRung
from .recipe_store import RecipeStore

POLICY_ID = "redi10b_stable_baseline_qualification_v1"
CRITERIA_VERSION = 1
BLOCKED_TRANSITION = "VALIDATION_BLOCKED"

MANDATORY_GATES: tuple[str, ...] = (
    "private_verification",
    "integration",
    "reviewer",
    "scope_security",
)


class StableBaselineError(ValueError):
    """A qualification rule, scope, or lifecycle rule was violated."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. Predeclared V0 stability criteria (versioned, hash-frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StableBaselineCriteria:
    """V0 stability floors.  Change => new version; never edited in place."""

    version: int
    preregistered_before_results: bool
    min_distinct_products: int
    min_verified_transfers: int
    mandatory_gates: tuple[str, ...]
    require_all_mandatory_gates: bool
    require_quality_non_regression: bool
    require_replay_x2_per_product: bool
    require_rollback_target_quality_evidence: bool
    require_evidence_ledger_completeness: bool
    validity_window_max_days: int
    stability_window_min_days: int
    require_explicit_approval_validated: bool
    require_explicit_approval_stable: bool
    justifications: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.preregistered_before_results:
            raise StableBaselineError("criteria must be preregistered before results")
        if self.min_distinct_products < 2:
            raise StableBaselineError(
                "a stability claim requires replication beyond a single product"
            )
        if self.min_verified_transfers < self.min_distinct_products:
            raise StableBaselineError("one verified transfer per product is required")
        if tuple(self.mandatory_gates) != MANDATORY_GATES or not self.require_all_mandatory_gates:
            raise StableBaselineError("mandatory gates are fixed by policy")
        if not all(
            (
                self.require_quality_non_regression,
                self.require_replay_x2_per_product,
                self.require_rollback_target_quality_evidence,
                self.require_evidence_ledger_completeness,
                self.require_explicit_approval_validated,
                self.require_explicit_approval_stable,
            )
        ):
            raise StableBaselineError("V0 stability requirements cannot be weakened")
        if self.validity_window_max_days <= 0 or self.stability_window_min_days <= 0:
            raise StableBaselineError("bounded windows are required")
        if self.stability_window_min_days > self.validity_window_max_days:
            raise StableBaselineError("stability dwell must fit inside the validity window")

    @property
    def criteria_hash(self) -> str:
        return digest(asdict(self))


STABLE_BASELINE_CRITERIA_V0 = StableBaselineCriteria(
    version=CRITERIA_VERSION,
    preregistered_before_results=True,
    min_distinct_products=3,
    min_verified_transfers=3,
    mandatory_gates=MANDATORY_GATES,
    require_all_mandatory_gates=True,
    require_quality_non_regression=True,
    require_replay_x2_per_product=True,
    require_rollback_target_quality_evidence=True,
    require_evidence_ledger_completeness=True,
    validity_window_max_days=90,
    stability_window_min_days=14,
    require_explicit_approval_validated=True,
    require_explicit_approval_stable=True,
    justifications={
        "min_distinct_products": (
            "the store's confidence model itself marks single-product evidence "
            "LOW/SINGLE_PRODUCT_EVIDENCE; three independent products is the "
            "smallest set that demonstrates cross-product generalization with "
            "majority agreement"
        ),
        "min_verified_transfers": (
            "one fully verified paired holdout per product; fewer cannot "
            "support a stability claim"
        ),
        "mandatory_gates": (
            "identical to the frozen MCW/transfer verification gates: private, "
            "integration, reviewer, scope/security"
        ),
        "require_replay_x2_per_product": (
            "REDI-07..10 replay discipline: evidence must reconstruct from "
            "persisted bytes alone, twice identically"
        ),
        "require_rollback_target_quality_evidence": (
            "a rollback path is only real if the target itself carries "
            "verified quality evidence (REDI-08 v5-failed finding)"
        ),
        "validity_window_max_days": (
            "stability is bounded; unbounded claims are forbidden by the "
            "research charter"
        ),
        "stability_window_min_days": (
            "a VALIDATED recipe must dwell without recorded regression before "
            "STABLE; 14 days is the minimum observation window"
        ),
        "approval_rungs": (
            "REDI-08/09 governance: promotion events require explicit "
            "human/audit approval citing the exact evidence hash"
        ),
    },
)


# ---------------------------------------------------------------------------
# 1b. REDI-10C0: criteria v2 — bootstrap rollback fix ONLY (v1 immutable)
# ---------------------------------------------------------------------------
# v1 requires the rollback target to carry its own verified quality evidence.
# For the FIRST-EVER stable baseline the canonical pre-promotion operational
# state is "no promoted recipe" (fallback-worker path), which is not a recipe
# and cannot carry recipe-level verified quality evidence.  Under v1 the first
# baseline is therefore blocked indefinitely unless some prior experimental
# version independently holds verified evidence (none does in REDI history),
# or actors fabricate a no-op "rollback recipe" — gaming.  v2 fixes ONLY this:
# a first-baseline qualification may roll back to an explicitly captured,
# owner-approved, restoration-proven prior operational state.  Later
# promotions keep the v1 rule unchanged.  v1 stays hash-frozen and valid.


@dataclass(frozen=True)
class BootstrapPriorState:
    """Exact pre-promotion operational state for a first-baseline bootstrap."""

    state_id: str
    captured_state_hash: str
    restoration_procedure_hash: str
    restoration_replay_x2_identical: bool
    fallback_description: str
    owner_approved_by: str
    owner_audit_reference: str

    def __post_init__(self) -> None:
        if not self.state_id or len(self.captured_state_hash) != 64 or len(self.restoration_procedure_hash) != 64:
            raise StableBaselineError(
                "prior operational state capture and restoration procedure hashes are required"
            )
        if not self.restoration_replay_x2_identical:
            raise StableBaselineError("deterministic restoration proof (replay x2) is required")
        if not self.fallback_description:
            raise StableBaselineError("fallback description of the prior operational state is required")
        if not self.owner_approved_by or not self.owner_audit_reference:
            raise StableBaselineError("prior-state capture requires explicit owner approval")


@dataclass(frozen=True)
class StableBaselineCriteriaV2(StableBaselineCriteria):
    """v2 = v1 + bootstrap rollback rule ONLY; every v1 floor is inherited."""

    allow_prior_operational_state_rollback: bool = True
    require_restoration_replay_proof: bool = True
    require_owner_approved_prior_state: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        if not (
            self.allow_prior_operational_state_rollback
            and self.require_restoration_replay_proof
            and self.require_owner_approved_prior_state
        ):
            raise StableBaselineError("the bootstrap rollback rule cannot be weakened")


STABLE_BASELINE_CRITERIA_V2 = StableBaselineCriteriaV2(
    version=2,
    preregistered_before_results=True,
    min_distinct_products=STABLE_BASELINE_CRITERIA_V0.min_distinct_products,
    min_verified_transfers=STABLE_BASELINE_CRITERIA_V0.min_verified_transfers,
    mandatory_gates=STABLE_BASELINE_CRITERIA_V0.mandatory_gates,
    require_all_mandatory_gates=True,
    require_quality_non_regression=True,
    require_replay_x2_per_product=True,
    require_rollback_target_quality_evidence=True,
    require_evidence_ledger_completeness=True,
    validity_window_max_days=STABLE_BASELINE_CRITERIA_V0.validity_window_max_days,
    stability_window_min_days=STABLE_BASELINE_CRITERIA_V0.stability_window_min_days,
    require_explicit_approval_validated=True,
    require_explicit_approval_stable=True,
    justifications={
        **STABLE_BASELINE_CRITERIA_V0.justifications,
        "bootstrap_rollback": (
            "for the FIRST-EVER stable baseline the pre-promotion operational "
            "state is 'no promoted recipe' (fallback path), which cannot carry "
            "recipe-level quality evidence; v2 requires instead an exactly "
            "captured, owner-approved prior state with a deterministic, "
            "replay-proven restoration procedure. Later promotions keep the "
            "v1 quality-verified-recipe rollback rule unchanged"
        ),
    },
)


def _validate_bootstrap_state(state: Any) -> dict[str, Any]:
    if isinstance(state, BootstrapPriorState):
        return asdict(state)
    if isinstance(state, Mapping):
        return asdict(BootstrapPriorState(**state))
    raise StableBaselineError("bootstrap prior operational state record required")


# ---------------------------------------------------------------------------
# 2. Frozen evidence records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductHoldoutEvidence:
    """One independent product's frozen transfer/holdout evidence."""

    product_id: str
    transfer_evidence_hash: str
    verified: bool
    gates: Mapping[str, bool]
    replay_x2_identical: bool
    quality_non_regression: bool
    provider_calls: int
    store_metadata_reconciled: bool

    def __post_init__(self) -> None:
        if not self.product_id or len(self.transfer_evidence_hash) != 64:
            raise StableBaselineError("product identity and evidence hash required")
        if any(not isinstance(v, bool) for v in self.gates.values()):
            raise StableBaselineError("gate outcomes must be booleans")
        if self.provider_calls < 0:
            raise StableBaselineError(
                "provider calls cannot be negative; the qualification DECISION is "
                "always offline even when acquisition used live calls"
            )


@dataclass(frozen=True)
class RecipeRef:
    recipe_id: str
    version: int
    canonical_hash: str
    lifecycle: str

    def __post_init__(self) -> None:
        if not self.recipe_id or self.version < 1 or len(self.canonical_hash) != 64:
            raise StableBaselineError("recipe identity, version, and hash required")


@dataclass(frozen=True)
class QualificationManifest:
    """Versioned contract binding recipe, criteria, rollback, and window."""

    qualification_id: str
    version: int
    recipe: RecipeRef
    criteria: StableBaselineCriteria
    rollback_target: RecipeRef
    station_id: str
    task_class: str
    valid_from: str
    valid_until: str
    first_baseline: bool = False

    def __post_init__(self) -> None:
        if not self.qualification_id or self.version < 1:
            raise StableBaselineError("versioned qualification identity required")
        if self.rollback_target == self.recipe or (
            self.rollback_target.canonical_hash == self.recipe.canonical_hash
        ):
            raise StableBaselineError("rollback target must be a distinct immutable version")
        if not self.station_id or not self.task_class:
            raise StableBaselineError("exact station/task scope required")
        if not self.valid_from or not self.valid_until or self.valid_from >= self.valid_until:
            raise StableBaselineError("bounded validity window required")

    @property
    def manifest_hash(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class QualificationApproval:
    approved_by: str
    audit_reference: str
    evidence_hash: str
    rung: str

    def __post_init__(self) -> None:
        if not self.approved_by or not self.audit_reference or len(self.evidence_hash) != 64:
            raise StableBaselineError("explicit human/audit approval with evidence hash required")
        if self.rung not in {"VALIDATED", "STABLE", "ROLLED_BACK"}:
            raise StableBaselineError("approval rung must be a promotion/rollback rung")


@dataclass(frozen=True)
class StabilityWindow:
    days_observed: int
    regression_events: int
    window_evidence_hash: str

    def __post_init__(self) -> None:
        if self.days_observed < 0 or self.regression_events < 0:
            raise StableBaselineError("stability window counts cannot be negative")
        if len(self.window_evidence_hash) != 64:
            raise StableBaselineError("window evidence hash required")


# ---------------------------------------------------------------------------
# 3. Criteria evaluation + gap analysis (pure; read-only)
# ---------------------------------------------------------------------------


def evaluate_criteria(
    manifest: QualificationManifest,
    evidence: Sequence[ProductHoldoutEvidence],
    *,
    prior_operational_state: Any = None,
    rollback_target_quality_evidence_hash: str | None = None,
) -> dict[str, Any]:
    """Judge a frozen evidence bundle against the frozen criteria; no side effects.

    v1 criteria keep their exact REDI-10B semantics.  v2 criteria add the
    rollback-path check: bootstrap qualifications must present a captured,
    owner-approved prior operational state with restoration proof; later
    promotions must pin verified quality evidence for the recipe rollback
    target.  Missing rollback evidence is a recorded gap, never an exception.
    """
    if getattr(manifest.criteria, "allow_prior_operational_state_rollback", False):
        return _evaluate_criteria_v2(
            manifest, evidence, prior_operational_state, rollback_target_quality_evidence_hash
        )
    return _evaluate_criteria_v1(manifest, evidence)


def _evaluate_criteria_v1(
    manifest: QualificationManifest, evidence: Sequence[ProductHoldoutEvidence]
) -> dict[str, Any]:
    c = manifest.criteria
    products = {e.product_id for e in evidence}
    distinct = len(products)
    verified = [e for e in evidence if e.verified]
    gates_ok = [e for e in verified if all(e.gates.get(g) is True for g in c.mandatory_gates)]
    replay_ok = [e for e in verified if e.replay_x2_identical]
    nonreg_ok = [e for e in verified if e.quality_non_regression]
    complete_ok = [e for e in verified if e.store_metadata_reconciled and e.transfer_evidence_hash]

    full = [
        e
        for e in evidence
        if e.verified
        and all(e.gates.get(g) is True for g in c.mandatory_gates)
        and e.replay_x2_identical
        and e.quality_non_regression
        and e.store_metadata_reconciled
    ]

    checks = {
        "INDEPENDENT_PRODUCT_COVERAGE": {
            "threshold": c.min_distinct_products,
            "observed": distinct,
            "met": distinct >= c.min_distinct_products,
        },
        "VERIFIED_TRANSFERS": {
            "threshold": c.min_verified_transfers,
            "observed": len(full),
            "met": len(full) >= c.min_verified_transfers,
        },
        "QUALITY_NON_REGRESSION": {
            "threshold": c.min_verified_transfers,
            "observed": len(nonreg_ok),
            "met": len(nonreg_ok) >= c.min_verified_transfers,
        },
        "REPLAY_X2_PER_PRODUCT": {
            "threshold": c.min_verified_transfers,
            "observed": len(replay_ok),
            "met": len(replay_ok) >= c.min_verified_transfers,
        },
        "MANDATORY_GATES": {
            "threshold": c.min_verified_transfers,
            "observed": len(gates_ok),
            "met": len(gates_ok) >= c.min_verified_transfers,
        },
        "EVIDENCE_COMPLETENESS": {
            "threshold": c.min_verified_transfers,
            "observed": len(complete_ok),
            "met": len(complete_ok) >= c.min_verified_transfers,
        },
    }
    failed = sorted(name for name, row in checks.items() if not row["met"])
    return {
        "criteria_version": c.version,
        "criteria_hash": c.criteria_hash,
        "manifest_hash": manifest.manifest_hash,
        "checks": checks,
        "failed_criteria": failed,
        "criteria_met": not failed,
    }


def _evaluate_criteria_v2(
    manifest: QualificationManifest,
    evidence: Sequence[ProductHoldoutEvidence],
    prior_operational_state: Any,
    rollback_target_quality_evidence_hash: str | None,
) -> dict[str, Any]:
    result = _evaluate_criteria_v1(manifest, evidence)
    c = manifest.criteria
    if manifest.first_baseline:
        if prior_operational_state is None:
            result["checks"]["BOOTSTRAP_ROLLBACK_PATH"] = {
                "threshold": "captured + owner-approved + restoration replay x2",
                "observed": "missing",
                "met": False,
            }
        else:
            state = _validate_bootstrap_state(prior_operational_state)
            met = bool(state["restoration_replay_x2_identical"]) and bool(
                state["owner_approved_by"]
            )
            result["checks"]["BOOTSTRAP_ROLLBACK_PATH"] = {
                "threshold": "captured + owner-approved + restoration replay x2",
                "observed": state["state_id"],
                "met": met,
            }
    else:
        met = rollback_target_quality_evidence_hash is not None and len(
            rollback_target_quality_evidence_hash
        ) == 64
        result["checks"]["ROLLBACK_TARGET_QUALITY_EVIDENCE"] = {
            "threshold": "verified quality evidence pinned for the recipe rollback target",
            "observed": "pinned" if rollback_target_quality_evidence_hash else "missing",
            "met": met,
        }
    result["failed_criteria"] = sorted(
        name for name, row in result["checks"].items() if not row["met"]
    )
    result["criteria_met"] = not result["failed_criteria"]
    return result


# ---------------------------------------------------------------------------
# 4. Qualification ledger (hash-chained JSONL; same discipline as REDI-10A)
# ---------------------------------------------------------------------------


class QualificationLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        previous = "0" * 64
        for line in self.path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["previous_hash"] != previous or row["hash"] != digest(
                {k: v for k, v in row.items() if k != "hash"}
            ):
                raise StableBaselineError("qualification ledger hash chain is invalid")
            previous = row["hash"]
            out.append(row)
        return out

    def append(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        events = self.events()
        row = {
            "sequence": len(events) + 1,
            "event_type": event_type,
            "payload": dict(payload),
            "previous_hash": events[-1]["hash"] if events else "0" * 64,
        }
        row["hash"] = digest(row)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(canonical(row) + "\n")
        return row

    def replay(self) -> dict[str, Any]:
        """Reconstruct state from persisted bytes alone; no current-policy reads."""
        rows = self.events()
        if not rows or rows[0]["event_type"] != "MANIFEST_FROZEN":
            raise StableBaselineError("replay requires a frozen manifest as the first event")
        payload = rows[0]["payload"]
        if payload["manifest_hash"] != digest(payload["manifest"]):
            raise StableBaselineError("manifest identity drift in replay")
        state = State.SHADOW
        for row in rows[1:]:
            kind, data = row["event_type"], row["payload"]
            if kind in {"VALIDATION_BLOCKED", "STABILITY_WINDOW", "PROVENANCE_BOUND", "DWELL_STARTED"}:
                continue
            if kind != "STATE_CHANGED":
                raise StableBaselineError(f"unknown qualification event: {kind}")
            if data["from"] != state.value:
                raise StableBaselineError("invalid lifecycle transition in replay")
            if data["manifest_hash"] != digest(rows[0]["payload"]["manifest"]):
                raise StableBaselineError("state transition uses different manifest")
            state = State(data["to"])
        return {
            "manifest": payload["manifest"],
            "manifest_hash": payload["manifest_hash"],
            "bootstrap": payload.get("bootstrap"),
            "first_baseline": payload.get("first_baseline", False),
            "state": state.value,
            "event_count": len(rows),
            "last_hash": rows[-1]["hash"],
            "blocked_evaluations": [
                r["payload"] for r in rows if r["event_type"] == "VALIDATION_BLOCKED"
            ],
            "stability_windows": [
                r["payload"] for r in rows if r["event_type"] == "STABILITY_WINDOW"
            ],
            "replay_dependencies": {
                "provider_calls": 0,
                "network": 0,
                "current_registry": False,
                "current_config": False,
            },
        }


class State(StrEnum):
    SHADOW = "SHADOW"
    ELIGIBLE_FOR_VALIDATION = "ELIGIBLE_FOR_VALIDATION"
    VALIDATED = "VALIDATED"
    STABLE = "STABLE"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    ROLLED_BACK = "ROLLED_BACK"


_ALLOWED: Mapping[State, frozenset[State]] = {
    State.SHADOW: frozenset({State.ELIGIBLE_FOR_VALIDATION, State.VALIDATION_REJECTED}),
    State.ELIGIBLE_FOR_VALIDATION: frozenset({State.VALIDATED, State.VALIDATION_REJECTED}),
    State.VALIDATED: frozenset({State.STABLE, State.ROLLED_BACK}),
    State.STABLE: frozenset({State.ROLLED_BACK}),
}


# ---------------------------------------------------------------------------
# 5. Governor: explicit, approval-gated lifecycle; no silent promotion
# ---------------------------------------------------------------------------


class StableBaselineGovernor:
    def __init__(
        self,
        manifest: QualificationManifest,
        ledger: QualificationLedger,
        *,
        bootstrap: BootstrapPriorState | None = None,
        rollback_target_quality_evidence_hash: str | None = None,
    ) -> None:
        if bootstrap is not None:
            if not manifest.first_baseline:
                raise StableBaselineError(
                    "bootstrap rollback requires a first_baseline qualification manifest"
                )
            if not getattr(manifest.criteria, "allow_prior_operational_state_rollback", False):
                raise StableBaselineError(
                    "first-baseline bootstrap requires criteria v2; v1 is not silently weakened"
                )
        self.manifest = manifest
        self.ledger = ledger
        self.bootstrap = bootstrap
        self.rollback_target_quality_evidence_hash = rollback_target_quality_evidence_hash
        events = ledger.events()
        if not events:
            frozen = {
                "manifest": asdict(manifest),
                "manifest_hash": manifest.manifest_hash,
                "bootstrap": asdict(bootstrap) if bootstrap else None,
                "first_baseline": manifest.first_baseline,
            }
            ledger.append("MANIFEST_FROZEN", frozen)
        elif events[0]["payload"].get("manifest_hash") != manifest.manifest_hash:
            raise StableBaselineError("frozen qualification manifest drift")

    @property
    def state(self) -> State:
        return State(self.ledger.replay()["state"])

    def _transition(self, target: State, evidence: Mapping[str, Any], approval: QualificationApproval | None) -> None:
        current = self.state
        if target not in _ALLOWED.get(current, frozenset()):
            raise StableBaselineError(f"invalid lifecycle transition {current}->{target}")
        if target in (State.VALIDATED, State.STABLE, State.ROLLED_BACK):
            if approval is None:
                raise StableBaselineError(f"{target.value} requires explicit human/audit approval")
            expected_rung = {State.VALIDATED: "VALIDATED", State.STABLE: "STABLE", State.ROLLED_BACK: "ROLLED_BACK"}[target]
            if approval.rung != expected_rung:
                raise StableBaselineError("approval rung does not match the target rung")
        if target in (State.VALIDATED, State.STABLE) and (
            approval is None or approval.evidence_hash != self._expected_approval_evidence(target)
        ):
            raise StableBaselineError("approval does not cite the exact qualification evidence")
        if target is State.ROLLED_BACK:
            if approval is None:
                raise StableBaselineError("rollback requires explicit human/audit approval")
            if approval.rung != "ROLLED_BACK":
                raise StableBaselineError("approval rung does not match the target rung")
            if self.bootstrap is not None:
                if approval.evidence_hash != self.bootstrap.captured_state_hash:
                    raise StableBaselineError(
                        "bootstrap rollback approval must pin the exact captured prior-state hash"
                    )
            elif approval.evidence_hash != self.manifest.rollback_target.canonical_hash:
                raise StableBaselineError("rollback approval must pin the exact immutable prior hash")
        self.ledger.append(
            "STATE_CHANGED",
            {
                "from": current.value,
                "to": target.value,
                "evidence": dict(evidence),
                "approval": asdict(approval) if approval else None,
                "manifest_hash": self.manifest.manifest_hash,
            },
        )

    def _expected_approval_evidence(self, target: State) -> str:
        """The exact evidence hash an approval must cite for each rung."""
        if target is State.VALIDATED:
            return digest({"manifest_hash": self.manifest.manifest_hash, "rung": "VALIDATED"})
        windows = self.ledger.replay()["stability_windows"]
        return windows[-1]["window_evidence_hash"]

    def evaluate(self, evidence: Sequence[ProductHoldoutEvidence]) -> dict[str, Any]:
        """Criteria evaluation from SHADOW.  Unmet criteria block; nothing promotes."""
        if self.state is not State.SHADOW:
            raise StableBaselineError("criteria evaluation requires SHADOW state")
        result = evaluate_criteria(
            self.manifest,
            evidence,
            prior_operational_state=self.bootstrap,
            rollback_target_quality_evidence_hash=self.rollback_target_quality_evidence_hash,
        )
        if result["criteria_met"]:
            self._transition(State.ELIGIBLE_FOR_VALIDATION, result, None)
        else:
            self.ledger.append(
                "VALIDATION_BLOCKED",
                {**result, "state_unchanged": State.SHADOW.value, "no_promotion": True},
            )
        return result

    def reject(self, regression: Mapping[str, Any]) -> None:
        if self.state not in (State.SHADOW, State.ELIGIBLE_FOR_VALIDATION):
            raise StableBaselineError("validation rejection applies before VALIDATED")
        self._transition(State.VALIDATION_REJECTED, {"regression": dict(regression)}, None)

    def bind_provenance(self, provenance_record_hash: str, audit_reference: str) -> dict[str, Any]:
        """Bind the qualification to a frozen provenance record (forward-only)."""
        if len(provenance_record_hash) != 64:
            raise StableBaselineError("provenance record hash required")
        if self.state not in (State.SHADOW, State.ELIGIBLE_FOR_VALIDATION, State.VALIDATED):
            raise StableBaselineError("provenance binding applies before STABLE")
        row = {
            "provenance_record_hash": provenance_record_hash,
            "audit_reference": audit_reference,
            "bound_at_state": self.state.value,
        }
        self.ledger.append("PROVENANCE_BOUND", row)
        return row

    def start_dwell(self, *, started_date: str, evidence_hash: str) -> dict[str, Any]:
        """Start the frozen regression-free dwell after VALIDATED.

        The dwell is recorded with its predeclared length; STABLE cannot be
        approved until a completed window with zero regression events exists.
        """
        if self.state is not State.VALIDATED:
            raise StableBaselineError("dwell starts only after VALIDATED")
        if len(evidence_hash) != 64:
            raise StableBaselineError("dwell start evidence hash required")
        row = {
            "started_date": started_date,
            "required_days": self.manifest.criteria.stability_window_min_days,
            "regression_events_allowed": 0,
            "dwell_evidence_hash": evidence_hash,
        }
        self.ledger.append("DWELL_STARTED", row)
        return row

    def approve_validated(self, approval: QualificationApproval) -> None:
        if self.state is not State.ELIGIBLE_FOR_VALIDATION:
            raise StableBaselineError("VALIDATED requires ELIGIBLE_FOR_VALIDATION state")
        self._transition(State.VALIDATED, {"rung": "VALIDATED"}, approval)

    def record_stability_window(self, window: StabilityWindow) -> None:
        if self.state is not State.VALIDATED:
            raise StableBaselineError("stability dwell applies to a VALIDATED recipe")
        if window.days_observed < self.manifest.criteria.stability_window_min_days:
            raise StableBaselineError("stability dwell below the predeclared minimum")
        if window.regression_events > 0:
            raise StableBaselineError("regression during the dwell forbids STABLE promotion")
        self.ledger.append("STABILITY_WINDOW", asdict(window))

    def approve_stable(self, approval: QualificationApproval) -> None:
        if self.state is not State.VALIDATED:
            raise StableBaselineError("STABLE requires VALIDATED state")
        windows = self.ledger.replay()["stability_windows"]
        if not windows:
            raise StableBaselineError("STABLE requires a completed stability dwell")
        self._transition(State.STABLE, {"rung": "STABLE"}, approval)

    def rollback(self, regression: Mapping[str, Any], approval: QualificationApproval) -> None:
        if self.state not in (State.VALIDATED, State.STABLE):
            raise StableBaselineError("rollback applies after VALIDATED")
        if self.bootstrap is not None:
            target: Any = {"prior_operational_state": asdict(self.bootstrap)}
        else:
            target = asdict(self.manifest.rollback_target)
        self._transition(
            State.ROLLED_BACK,
            {"regression": dict(regression), "target": target},
            approval,
        )


# ---------------------------------------------------------------------------
# 6. Read-only store baseline classification + v6 gap audit
# ---------------------------------------------------------------------------


def classify_store_baseline(store: RecipeStore, recipe_id: str) -> dict[str, Any]:
    """Read-only: a successful transfer is not a stable-production pointer."""
    versions = store.list_versions(recipe_id)
    selected = store.current_selected_version(recipe_id)
    validated = [v for v in versions if v.lifecycle_state == RecipeRung.VALIDATED]
    events = store.events(recipe_id)
    approved = any(
        e["event_type"] == "RECIPE_PROMOTED" and e.get("to_version") == selected and e.get("approval_json")
        for e in events
    )
    stable = (
        selected is not None
        and any(v.recipe.version == selected for v in validated)
        and approved
    )
    return {
        "classification": "STABLE_BASELINE_EXISTS" if stable else "STABLE_BASELINE_NOT_YET_ESTABLISHED",
        "recipe_id": recipe_id,
        "selected_version": selected,
        "lifecycles": {v.recipe.version: v.lifecycle_state.value for v in versions},
        "validated_versions": [v.recipe.version for v in validated],
        "approved_production_promotion": approved,
    }


# Accepted REDI-08/09/10 facts for the v6 audit (pinned, read-only).
V6_RECIPE_ID = "recipe_cold_39553c39ceb593e2bc24775be1c420e2"
V6_STORE_EVIDENCE_HASH = "de3aec903f7cf43fee3cf09f65503b8a82c2b81727cdcfe5515926af75dd8e3e"
V6_DISTINCT_VERIFIED_PRODUCTS = 1  # the single redi08c14 verified transfer
V6_ROLLBACK_TARGET_VERSION = 5
V6_ROLLBACK_TARGET_VERIFIED = False  # v5 holdout FAILED (accepted owner runs)


def v6_qualification_audit(store: RecipeStore) -> dict[str, Any]:
    """Read-only: judge the current v6 evidence against the frozen V0 criteria."""
    baseline = classify_store_baseline(store, V6_RECIPE_ID)
    versions = store.list_versions(V6_RECIPE_ID)
    v6 = next((v for v in versions if v.recipe.version == 6), None)
    manifest = QualificationManifest(
        qualification_id="redi10b_v6_qualification_v1",
        version=1,
        recipe=RecipeRef(V6_RECIPE_ID, 6, v6.canonical_hash, v6.lifecycle_state.value) if v6 else RecipeRef(V6_RECIPE_ID, 6, "0" * 64, "shadow"),
        criteria=STABLE_BASELINE_CRITERIA_V0,
        rollback_target=RecipeRef(
            V6_RECIPE_ID,
            V6_ROLLBACK_TARGET_VERSION,
            digest({"recipe_id": V6_RECIPE_ID, "version": V6_ROLLBACK_TARGET_VERSION, "pin": "redi08c-lineage"}),
            "shadow",
        ),
        station_id="station_mcw_implementation_v0",
        task_class="mcw_python_component_v1",
        valid_from="2026-09-25T00:00:00Z",
        valid_until="2026-12-24T00:00:00Z",
    )
    evidence = [
        ProductHoldoutEvidence(
            product_id="holdout_product_single",
            transfer_evidence_hash=V6_STORE_EVIDENCE_HASH,
            verified=True,
            gates={g: True for g in MANDATORY_GATES},
            replay_x2_identical=True,
            quality_non_regression=True,
            provider_calls=0,
            store_metadata_reconciled=False,
        )
    ]
    result = evaluate_criteria(manifest, evidence)
    rollback_check = {
        "rollback_target_version": V6_ROLLBACK_TARGET_VERSION,
        "target_quality_evidence": V6_ROLLBACK_TARGET_VERIFIED,
        "met": V6_ROLLBACK_TARGET_VERIFIED,
    }
    gaps = list(result["failed_criteria"])
    if not rollback_check["met"]:
        gaps.append("ROLLBACK_TARGET_QUALITY_EVIDENCE")
    v2_manifest = replace(manifest, criteria=STABLE_BASELINE_CRITERIA_V2, first_baseline=True)
    v2_result = evaluate_criteria(v2_manifest, evidence, prior_operational_state=None)
    v2_gaps = list(v2_result["failed_criteria"])
    v2_gaps.append("BOOTSTRAP_ROLLBACK_PATH: no captured, owner-approved prior operational "
                   "state with a restoration replay proof exists for the first-ever baseline")
    return {
        "store_baseline": baseline,
        "manifest_hash": manifest.manifest_hash,
        "criteria_hash": result["criteria_hash"],
        "criteria_result": result,
        "rollback_check": rollback_check,
        "eligible": False,
        "missing_evidence": gaps,
        "v2_audit": {
            "criteria_version": 2,
            "criteria_hash": v2_result["criteria_hash"],
            "manifest_hash": v2_manifest.manifest_hash,
            "failed_criteria": v2_result["failed_criteria"],
            "eligible": False,
            "missing_evidence": v2_gaps,
            "bootstrap_note": (
                "under v2 the first-baseline bootstrap rollback path is OPEN in "
                "principle (capture prior operational state + restoration proof "
                "+ owner approval), but no such record exists for v6, and the "
                "product-coverage/transfer criteria still fail unchanged"
            ),
        },
        "note": "read-only audit; v6 is NOT promoted and the store is never mutated",
    }


__all__ = [
    "BLOCKED_TRANSITION",
    "BootstrapPriorState",
    "STABLE_BASELINE_CRITERIA_V2",
    "StableBaselineCriteriaV2",
    "ProductHoldoutEvidence",
    "QualificationApproval",
    "QualificationLedger",
    "QualificationManifest",
    "RecipeRef",
    "STABLE_BASELINE_CRITERIA_V0",
    "StableBaselineCriteria",
    "StableBaselineError",
    "StableBaselineGovernor",
    "StabilityWindow",
    "State",
    "V6_RECIPE_ID",
    "classify_store_baseline",
    "digest",
    "evaluate_criteria",
    "v6_qualification_audit",
]
