"""REDI-10A offline continual-optimization control plane.

This module cannot execute a worker or update RecipeStore.  It records frozen
contracts and observations, then derives decisions from those records.  A
PROMOTED state here is a dry, synthetic state, never a production recipe rung.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .domain import RecipeRung
from .recipe_store import RecipeStore


POLICY_ID = "redi10a_offline_optimization_v1"
PRODUCTION_OPTIMIZATION_BLOCKED_UNTIL_STABLE_BASELINE = (
    "PRODUCTION_OPTIMIZATION_BLOCKED_UNTIL_STABLE_BASELINE"
)


class OptimizationError(ValueError):
    """A frozen control, scope, or lifecycle rule was violated."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _positive(value: int | float | str | Decimal, name: str) -> None:
    if Decimal(str(value)) <= 0:
        raise OptimizationError(f"{name} must be positive")


@dataclass(frozen=True)
class RecipeRef:
    recipe_id: str
    version: int
    canonical_hash: str
    lifecycle: str
    source: str = "synthetic"

    def __post_init__(self) -> None:
        if not self.recipe_id or self.version < 1 or len(self.canonical_hash) != 64:
            raise OptimizationError("recipe identity, version, and hash are required")
        if self.source not in {"synthetic", "recipe_store"}:
            raise OptimizationError("unknown recipe source")


def classify_baseline(store: RecipeStore, recipe_id: str) -> dict[str, Any]:
    """Read only.  A successful transfer is not a stable-production pointer."""

    versions = store.list_versions(recipe_id)
    selected = store.current_selected_version(recipe_id)
    validated = [v for v in versions if v.lifecycle_state == RecipeRung.VALIDATED]
    events = store.events(recipe_id)
    approved = any(
        e["event_type"] == "RECIPE_PROMOTED"
        and e["to_version"] == selected
        and e["approval_json"]
        for e in events
    )
    stable = selected is not None and any(
        v.recipe.version == selected for v in validated
    ) and approved
    return {
        "classification": "STABLE_BASELINE_EXISTS" if stable else "STABLE_BASELINE_NOT_YET_ESTABLISHED",
        "recipe_id": recipe_id,
        "selected_version": selected,
        "versions": [
            {
                "version": v.recipe.version,
                "hash": v.canonical_hash,
                "lifecycle": v.lifecycle_state.value,
                "parent_version": v.parent_version,
                "rollback_target_version": v.rollback_target_version,
                "confidence": v.confidence.to_dict(),
            }
            for v in versions
        ],
        "validated_versions": [v.recipe.version for v in validated],
        "approved_production_promotion": approved,
    }


@dataclass(frozen=True)
class ExplorationBudget:
    max_experiments: int
    max_provider_calls: int
    max_spend: str
    max_failed_candidates: int
    max_wall_seconds: int

    def __post_init__(self) -> None:
        for name in ("max_experiments", "max_failed_candidates", "max_wall_seconds"):
            _positive(getattr(self, name), name)
        if self.max_provider_calls < 0 or Decimal(self.max_spend) < 0:
            raise OptimizationError("provider-call and spend ceilings must be nonnegative")


@dataclass(frozen=True)
class PromotionRule:
    """Predeclared engineering gate; a formal scientific margin is separate."""

    rule_id: str
    version: int
    min_pairs: int
    require_all_quality_gates: bool
    require_verified_non_regression: bool
    require_lower_end_to_end_cost: bool
    threshold_source: str
    preregistered_before_results: bool
    replay_required: bool = True

    def __post_init__(self) -> None:
        if not self.rule_id or self.version < 1 or self.min_pairs < 1:
            raise OptimizationError("versioned promotion rule and positive pair floor required")
        if not self.threshold_source or not self.preregistered_before_results:
            raise OptimizationError("promotion rule must be preregistered before results")


@dataclass(frozen=True)
class OptimizationExperiment:
    experiment_id: str
    version: int
    baseline: RecipeRef
    candidate: RecipeRef
    treatment_delta: Mapping[str, Any]
    station_id: str
    task_class: str
    budget: ExplorationBudget
    evidence_requirements: tuple[str, ...]
    regression_gates: tuple[str, ...]
    promotion_rule: PromotionRule
    rollback_target: RecipeRef
    revalidation_policy_id: str
    valid_from: str
    valid_until: str
    mcw_evidence_ref: Mapping[str, str] | None = None
    synthetic_only: bool = True
    multi_variable_justification: str | None = None

    def __post_init__(self) -> None:
        if not self.synthetic_only:
            raise OptimizationError("REDI-10A permits synthetic/shadow experiments only")
        if not self.experiment_id or self.version < 1 or not self.station_id or not self.task_class:
            raise OptimizationError("versioned experiment and exact scope required")
        if not self.treatment_delta or (len(self.treatment_delta) != 1 and not self.multi_variable_justification):
            raise OptimizationError("exactly one material treatment variable is required unless explicitly justified")
        if self.baseline == self.candidate or self.baseline.canonical_hash == self.candidate.canonical_hash:
            raise OptimizationError("candidate must have a distinct immutable recipe hash")
        if self.rollback_target != self.baseline:
            raise OptimizationError("rollback target must be the exact baseline version/hash")
        if not self.evidence_requirements or not self.regression_gates or not self.revalidation_policy_id:
            raise OptimizationError("evidence, regression gates, and revalidation policy required")
        if not {"paired_verified_quality", "all_mandatory_gates", "replay"}.issubset(self.evidence_requirements):
            raise OptimizationError("paired quality, mandatory gates, and replay are required")
        if not {"private_verification", "integration", "reviewer", "scope_security"}.issubset(self.regression_gates):
            raise OptimizationError("private, integration, reviewer, and scope/security gates are required")
        if not self.valid_from or not self.valid_until or self.valid_from >= self.valid_until:
            raise OptimizationError("bounded validity window required")

    @property
    def manifest_hash(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class MCWReference:
    experiment: str
    manifest_hash: str
    result_hash: str
    station_id: str
    task_class: str
    condition: str
    model: str
    verified_count: int
    task_count: int
    state: str

    def applicable(self, station_id: str, task_class: str) -> bool:
        return (
            self.station_id == station_id
            and self.task_class == task_class
            and self.state == "CANDIDATE_SUFFICIENT"
            and self.verified_count <= self.task_count
            and len(self.manifest_hash) == 64
            and len(self.result_hash) == 64
        )


def eligible_downshift(
    *, manifest: OptimizationExperiment, evidence: MCWReference
) -> dict[str, Any]:
    eligible = evidence.applicable(manifest.station_id, manifest.task_class)
    if not eligible:
        raise OptimizationError("MCW evidence is not applicable to this station/task class")
    if "worker" not in next(iter(manifest.treatment_delta)).lower():
        raise OptimizationError("MCW evidence supports only a worker downshift candidate")
    if manifest.mcw_evidence_ref is None or any(
        manifest.mcw_evidence_ref.get(name) != getattr(evidence, name)
        for name in ("manifest_hash", "result_hash")
    ):
        raise OptimizationError("candidate must pin the MCW manifest and result hashes")
    return {
        "candidate_eligible": True,
        "source": evidence.experiment,
        "manifest_hash": evidence.manifest_hash,
        "result_hash": evidence.result_hash,
        "scope": {"station_id": evidence.station_id, "task_class": evidence.task_class},
        "warning": "Scoped prior only; no promotion or cross-station sufficiency claim",
    }


@dataclass(frozen=True)
class ShadowObservation:
    task_hash: str
    recipe: RecipeRef
    station_id: str
    task_class: str
    worker_id: str
    worker_version: str
    context_hash: str
    verified: bool
    gates: Mapping[str, bool]
    usage: Mapping[str, int]
    end_to_end_cost: str
    latency_ms: int
    failures: tuple[str, ...] = ()
    authoritative: bool = False

    def __post_init__(self) -> None:
        if not self.task_hash or not self.worker_id or not self.context_hash:
            raise OptimizationError("task, worker, and context identities required")
        if Decimal(self.end_to_end_cost) < 0 or self.latency_ms < 0:
            raise OptimizationError("nonnegative cost and latency required")
        if any(n < 0 for n in self.usage.values()):
            raise OptimizationError("nonnegative usage required")


@dataclass(frozen=True)
class ShadowPair:
    baseline: ShadowObservation
    candidate: ShadowObservation

    def validate(self, manifest: OptimizationExperiment) -> None:
        a, b = self.baseline, self.candidate
        if a.task_hash != b.task_hash or (a.station_id, a.task_class) != (b.station_id, b.task_class):
            raise OptimizationError("shadow pair must use the same input and scope")
        if (a.station_id, a.task_class) != (manifest.station_id, manifest.task_class):
            raise OptimizationError("shadow pair is outside manifest scope")
        if a.recipe != manifest.baseline or b.recipe != manifest.candidate or b.authoritative:
            raise OptimizationError("candidate cannot have output authority")
        if any(g not in a.gates or g not in b.gates for g in manifest.regression_gates):
            raise OptimizationError("required quality gates are missing")


@dataclass(frozen=True)
class OfflineFakeWorker:
    """Frozen fixture response map; no provider adapter or executable callback."""

    worker_id: str
    worker_version: str
    responses: Mapping[str, Mapping[str, Any]]

    def observe(
        self, *, task_hash: str, recipe: RecipeRef, station_id: str,
        task_class: str, authoritative: bool,
    ) -> ShadowObservation:
        if task_hash not in self.responses:
            raise OptimizationError("fake worker has no frozen response for task")
        row = self.responses[task_hash]
        return ShadowObservation(
            task_hash=task_hash, recipe=recipe, station_id=station_id,
            task_class=task_class, worker_id=self.worker_id,
            worker_version=self.worker_version, context_hash=str(row["context_hash"]),
            verified=bool(row["verified"]), gates=dict(row["gates"]),
            usage=dict(row["usage"]), end_to_end_cost=str(row["end_to_end_cost"]),
            latency_ms=int(row["latency_ms"]), failures=tuple(row.get("failures", ())),
            authoritative=authoritative,
        )


class State(StrEnum):
    CANDIDATE = "CANDIDATE"
    SHADOW = "SHADOW"
    ELIGIBLE_FOR_PROMOTION = "ELIGIBLE_FOR_PROMOTION"
    PROMOTED = "PROMOTED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Approval:
    approved_by: str
    audit_reference: str
    evidence_hash: str

    def __post_init__(self) -> None:
        if not self.approved_by or not self.audit_reference or len(self.evidence_hash) != 64:
            raise OptimizationError("explicit human/audit approval with evidence hash required")


@dataclass(frozen=True)
class DriftRule:
    rule_id: str
    version: int
    min_samples_per_window: int
    consecutive_bad_windows: int
    max_quality_drop: str
    max_cost_increase_fraction: str
    max_latency_increase_fraction: str
    max_task_share_shift: str

    def __post_init__(self) -> None:
        if not self.rule_id or self.version < 1 or self.min_samples_per_window < 2 or self.consecutive_bad_windows < 2:
            raise OptimizationError("versioned drift rule requires sustained sample windows")
        for name in ("max_quality_drop", "max_cost_increase_fraction", "max_latency_increase_fraction", "max_task_share_shift"):
            if Decimal(getattr(self, name)) <= 0:
                raise OptimizationError("drift thresholds must be positive")


@dataclass(frozen=True)
class DriftWindow:
    station_id: str
    task_class: str
    worker_id: str
    sample_count: int
    task_share: str
    model_version: str
    provider_version: str
    context_policy_hash: str
    gate_policy_hash: str
    quality_rate: str
    mean_cost: str
    mean_latency_ms: str


def detect_drift(rule: DriftRule, baseline: DriftWindow, windows: Sequence[DriftWindow]) -> dict[str, Any]:
    if not windows:
        return {"state": "INSUFFICIENT_EVIDENCE", "signals": []}
    if any((w.station_id, w.task_class, w.worker_id) != (baseline.station_id, baseline.task_class, baseline.worker_id) for w in windows):
        raise OptimizationError("drift windows must share exact scope")
    signals: set[str] = set()
    if any(w.sample_count < rule.min_samples_per_window for w in windows[-rule.consecutive_bad_windows:]):
        return {"state": "INSUFFICIENT_EVIDENCE", "signals": []}
    fixed = {
        "model_version": "MODEL_VERSION_CHANGED",
        "provider_version": "PROVIDER_VERSION_CHANGED",
        "context_policy_hash": "CONTEXT_POLICY_CHANGED",
        "gate_policy_hash": "GATE_BEHAVIOR_CHANGED",
    }
    for field_name, signal in fixed.items():
        if any(getattr(w, field_name) != getattr(baseline, field_name) for w in windows):
            signals.add(signal)
    recent = windows[-rule.consecutive_bad_windows:] if len(windows) >= rule.consecutive_bad_windows else ()
    checks = {
        "PRODUCT_CLASS_DISTRIBUTION_SHIFT": lambda w: abs(Decimal(w.task_share) - Decimal(baseline.task_share)) > Decimal(rule.max_task_share_shift),
        "QUALITY_REGRESSION": lambda w: Decimal(baseline.quality_rate) - Decimal(w.quality_rate) > Decimal(rule.max_quality_drop),
        "COST_SHIFT": lambda w: Decimal(w.mean_cost) > Decimal(baseline.mean_cost) * (1 + Decimal(rule.max_cost_increase_fraction)),
        "LATENCY_SHIFT": lambda w: Decimal(w.mean_latency_ms) > Decimal(baseline.mean_latency_ms) * (1 + Decimal(rule.max_latency_increase_fraction)),
    }
    for signal, predicate in checks.items():
        if recent and all(predicate(w) for w in recent):
            signals.add(signal)
    return {
        "state": "TARGETED_REVALIDATION_REQUIRED" if signals else "NO_DRIFT",
        "signals": sorted(signals),
        "scope": {"station_id": baseline.station_id, "task_class": baseline.task_class, "worker_id": baseline.worker_id},
        "policy": {"id": rule.rule_id, "version": rule.version},
    }


@dataclass(frozen=True)
class BudgetUse:
    experiments: int = 0
    provider_calls: int = 0
    spend: str = "0"
    failed_candidates: int = 0
    wall_seconds: int = 0

    def __post_init__(self) -> None:
        if min(self.experiments, self.provider_calls, self.failed_candidates, self.wall_seconds) < 0 or Decimal(self.spend) < 0:
            raise OptimizationError("budget use cannot be negative")

    def within(self, budget: ExplorationBudget) -> bool:
        return (
            self.experiments <= budget.max_experiments
            and self.provider_calls <= budget.max_provider_calls
            and Decimal(self.spend) <= Decimal(budget.max_spend)
            and self.failed_candidates <= budget.max_failed_candidates
            and self.wall_seconds <= budget.max_wall_seconds
        )


class OptimizationLedger:
    """Append-only, hash-chained JSONL; replay uses only persisted manifest/events."""

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
            if row["previous_hash"] != previous or row["hash"] != digest({k: v for k, v in row.items() if k != "hash"}):
                raise OptimizationError("optimization ledger hash chain is invalid")
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
        """No provider, network, current registry, current policy, or clock reads."""
        state = State.CANDIDATE
        active_recipe: dict[str, Any] | None = None
        manifest: dict[str, Any] | None = None
        pairs: list[dict[str, Any]] = []
        budget: dict[str, Any] | None = None
        drift: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        rows = self.events()
        if not rows or rows[0]["event_type"] != "MANIFEST_FROZEN":
            raise OptimizationError("replay requires a frozen manifest as the first event")
        for row in rows:
            kind, payload = row["event_type"], row["payload"]
            if kind == "MANIFEST_FROZEN":
                if manifest is not None or payload["manifest_hash"] != digest(payload["manifest"]):
                    raise OptimizationError("manifest identity drift in replay")
                manifest = payload["manifest"]
                active_recipe = manifest["baseline"]
            elif kind == "BUDGET_USED":
                if not BudgetUse(**payload).within(ExplorationBudget(**manifest["budget"])):
                    raise OptimizationError("replay encountered out-of-budget activity")
                budget = payload
            elif kind == "SHADOW_PAIR":
                if state != State.SHADOW or payload["candidate"]["authoritative"]:
                    raise OptimizationError("shadow evidence lacks isolation")
                pairs.append(payload)
            elif kind == "STATE_CHANGED":
                target = State(payload["to"])
                allowed = {
                    State.CANDIDATE: {State.SHADOW, State.REJECTED},
                    State.SHADOW: {State.ELIGIBLE_FOR_PROMOTION, State.REJECTED},
                    State.ELIGIBLE_FOR_PROMOTION: {State.PROMOTED, State.REJECTED},
                    State.PROMOTED: {State.ROLLED_BACK},
                }
                if payload["from"] != state.value or target not in allowed.get(state, set()):
                    raise OptimizationError("invalid lifecycle transition in replay")
                if payload["manifest_hash"] != digest(manifest):
                    raise OptimizationError("state transition uses different manifest")
                if target == State.PROMOTED and not payload.get("approval"):
                    raise OptimizationError("promotion lacks human/audit approval")
                if target == State.ROLLED_BACK and (
                    not payload.get("approval")
                    or payload["evidence"]["target"] != manifest["rollback_target"]
                ):
                    raise OptimizationError("rollback lacks exact immutable target")
                state = target
                decisions.append(payload)
                if state == State.PROMOTED:
                    active_recipe = manifest["candidate"] if manifest else None
                elif state == State.ROLLED_BACK:
                    active_recipe = manifest["rollback_target"] if manifest else None
            elif kind in {"DRIFT_OBSERVED", "REVALIDATION_DECIDED", "POST_PROMOTION_OBSERVATION"}:
                drift.append({"event_type": kind, **payload})
            elif kind != "BUDGET_EXHAUSTED":
                raise OptimizationError(f"unknown optimization event: {kind}")
        return {
            "manifest": manifest,
            "state": state.value,
            "active_recipe": active_recipe,
            "pairs": pairs,
            "budget": budget,
            "drift": drift,
            "decisions": decisions,
            "event_count": len(rows),
            "last_hash": rows[-1]["hash"],
            "replay_dependencies": {"provider_calls": 0, "network": 0, "current_registry": False, "current_config": False},
        }


class OfflineOptimizer:
    def __init__(self, manifest: OptimizationExperiment, ledger: OptimizationLedger) -> None:
        self.manifest = manifest
        self.ledger = ledger
        events = ledger.events()
        if not events:
            ledger.append("MANIFEST_FROZEN", {"manifest": asdict(manifest), "manifest_hash": manifest.manifest_hash})
        elif events[0]["payload"].get("manifest_hash") != manifest.manifest_hash:
            raise OptimizationError("frozen manifest drift")

    @property
    def state(self) -> State:
        return State(self.ledger.replay()["state"])

    def _transition(self, target: State, reason: str, evidence: Mapping[str, Any], approval: Approval | None = None) -> None:
        if any(e["event_type"] == "BUDGET_EXHAUSTED" for e in self.ledger.events()):
            raise OptimizationError("exploration budget exhausted; STOP")
        current = self.state
        allowed = {
            State.CANDIDATE: {State.SHADOW, State.REJECTED},
            State.SHADOW: {State.ELIGIBLE_FOR_PROMOTION, State.REJECTED},
            State.ELIGIBLE_FOR_PROMOTION: {State.PROMOTED, State.REJECTED},
            State.PROMOTED: {State.ROLLED_BACK},
        }
        if target not in allowed.get(current, set()):
            raise OptimizationError(f"invalid state transition {current}->{target}")
        if target == State.PROMOTED and approval is None:
            raise OptimizationError("promotion requires explicit human/audit approval")
        self.ledger.append("STATE_CHANGED", {
            "from": current.value, "to": target.value, "reason": reason,
            "evidence": dict(evidence), "approval": asdict(approval) if approval else None,
            "manifest_hash": self.manifest.manifest_hash,
        })

    def charge(self, use: BudgetUse) -> None:
        if any(e["event_type"] == "BUDGET_EXHAUSTED" for e in self.ledger.events()):
            raise OptimizationError("exploration budget exhausted; STOP")
        previous = self.ledger.replay()["budget"] or asdict(BudgetUse())
        cumulative = BudgetUse(
            experiments=previous["experiments"] + use.experiments,
            provider_calls=previous["provider_calls"] + use.provider_calls,
            spend=str(Decimal(previous["spend"]) + Decimal(use.spend)),
            failed_candidates=previous["failed_candidates"] + use.failed_candidates,
            wall_seconds=previous["wall_seconds"] + use.wall_seconds,
        )
        if not cumulative.within(self.manifest.budget):
            self.ledger.append("BUDGET_EXHAUSTED", {"attempted": asdict(cumulative), "retained_evidence": True})
            raise OptimizationError("exploration budget exhausted; STOP")
        if use.provider_calls != 0:
            raise OptimizationError("REDI-10A is offline: provider calls are forbidden")
        self.ledger.append("BUDGET_USED", asdict(cumulative))

    def enter_shadow(self) -> None:
        self._transition(State.SHADOW, "explicit shadow evaluation", {"manifest_hash": self.manifest.manifest_hash})

    def record_pair(self, pair: ShadowPair) -> None:
        if any(e["event_type"] == "BUDGET_EXHAUSTED" for e in self.ledger.events()):
            raise OptimizationError("exploration budget exhausted; STOP")
        if self.state != State.SHADOW:
            raise OptimizationError("shadow pairs require SHADOW state")
        pair.validate(self.manifest)
        if any(p["baseline"]["task_hash"] == pair.baseline.task_hash for p in self.ledger.replay()["pairs"]):
            raise OptimizationError("duplicate task input; no silent retry")
        self.ledger.append("SHADOW_PAIR", asdict(pair))

    def run_fake_shadow(
        self, task_input: Mapping[str, Any], baseline_worker: OfflineFakeWorker,
        candidate_worker: OfflineFakeWorker,
    ) -> ShadowPair:
        """Run both frozen fixture arms on one hashed input; candidate has no authority."""
        task_hash = digest(task_input)
        pair = ShadowPair(
            baseline_worker.observe(
                task_hash=task_hash, recipe=self.manifest.baseline,
                station_id=self.manifest.station_id, task_class=self.manifest.task_class,
                authoritative=True,
            ),
            candidate_worker.observe(
                task_hash=task_hash, recipe=self.manifest.candidate,
                station_id=self.manifest.station_id, task_class=self.manifest.task_class,
                authoritative=False,
            ),
        )
        self.record_pair(pair)
        return pair

    def evaluate(self) -> State:
        if self.state != State.SHADOW:
            raise OptimizationError("evaluation requires SHADOW state")
        rows = self.ledger.replay()["pairs"]
        rule = self.manifest.promotion_rule
        if len(rows) < rule.min_pairs:
            raise OptimizationError("preregistered evidence coverage is incomplete")
        replay_one = self.ledger.replay()
        replay_two = self.ledger.replay()
        if rule.replay_required and replay_one != replay_two:
            raise OptimizationError("paired evidence is not reproducible")
        failures: list[str] = []
        for row in rows:
            a, b = row["baseline"], row["candidate"]
            if rule.require_verified_non_regression and a["verified"] and not b["verified"]:
                failures.append("verified_quality_regression")
            if rule.require_all_quality_gates and any(a["gates"][g] and not b["gates"][g] for g in self.manifest.regression_gates):
                failures.append("quality_gate_regression")
            if rule.require_lower_end_to_end_cost and Decimal(b["end_to_end_cost"]) >= Decimal(a["end_to_end_cost"]):
                failures.append("cost_not_lower")
            if b["failures"]:
                failures.append("candidate_failure")
        evidence = {
            "pair_count": len(rows), "pair_hashes": [digest(r) for r in rows],
            "replay_hash": digest(replay_one), "rule": asdict(rule),
            "failures": sorted(set(failures)),
        }
        target = State.REJECTED if failures else State.ELIGIBLE_FOR_PROMOTION
        self._transition(target, "predeclared comparison", evidence)
        return target

    def promote(self, approval: Approval) -> None:
        if self.state != State.ELIGIBLE_FOR_PROMOTION:
            raise OptimizationError("candidate is not eligible")
        if approval.evidence_hash != digest(self.ledger.replay()["pairs"]):
            raise OptimizationError("approval does not cite the exact paired evidence")
        self._transition(State.PROMOTED, "synthetic dry promotion", {"pairs_hash": approval.evidence_hash}, approval)

    def rollback(self, *, regression: str, approval: Approval, evidence_hash: str | None = None) -> None:
        if self.state != State.PROMOTED or not regression:
            raise OptimizationError("rollback requires promoted candidate and recorded regression")
        if approval.evidence_hash != self.manifest.rollback_target.canonical_hash:
            raise OptimizationError("rollback approval must pin exact immutable prior recipe hash")
        self._transition(State.ROLLED_BACK, "regression-triggered dry rollback", {
            "regression": regression, "regression_evidence_hash": evidence_hash,
            "target": asdict(self.manifest.rollback_target)
        }, approval)

    def post_promotion_check(self, observation: ShadowObservation, *, rollback_approval: Approval) -> bool:
        """Apply preregistered gates to a dry promoted recipe; rollback on regression."""
        if self.state != State.PROMOTED or observation.recipe != self.manifest.candidate:
            raise OptimizationError("post-promotion check requires the exact promoted candidate")
        if rollback_approval.evidence_hash != self.manifest.rollback_target.canonical_hash:
            raise OptimizationError("post-promotion check requires preauthorized exact rollback target")
        if (observation.station_id, observation.task_class) != (self.manifest.station_id, self.manifest.task_class):
            raise OptimizationError("post-promotion observation is outside manifest scope")
        failure = not observation.verified or bool(observation.failures) or any(
            not observation.gates.get(g, False) for g in self.manifest.regression_gates
        )
        row = {"observation": asdict(observation), "regression": failure, "observation_hash": digest(asdict(observation))}
        self.ledger.append("POST_PROMOTION_OBSERVATION", row)
        if failure:
            self.rollback(regression="post-promotion quality/security gate regression", approval=rollback_approval, evidence_hash=row["observation_hash"])
        return failure

    def observe_drift(self, rule: DriftRule, baseline: DriftWindow, windows: Sequence[DriftWindow]) -> dict[str, Any]:
        result = detect_drift(rule, baseline, windows)
        self.ledger.append("DRIFT_OBSERVED", {
            "rule": asdict(rule), "baseline": asdict(baseline),
            "windows": [asdict(w) for w in windows], "result": result,
        })
        return result

    def targeted_revalidation(self, drift: Mapping[str, Any], *, evidence_refs: Sequence[str], decision: str) -> None:
        if drift.get("state") != "TARGETED_REVALIDATION_REQUIRED":
            raise OptimizationError("targeted revalidation requires a drift signal")
        if not any(
            row["event_type"] == "DRIFT_OBSERVED" and row["payload"]["result"] == drift
            for row in self.ledger.events()
        ):
            raise OptimizationError("drift trigger was not recorded")
        if decision not in {"RETAIN", "ROLLBACK_REQUIRED", "REVISE_CANDIDATE"} or not evidence_refs:
            raise OptimizationError("revalidation decision and evidence required")
        if drift.get("scope") != {
            "station_id": self.manifest.station_id, "task_class": self.manifest.task_class,
            "worker_id": drift.get("scope", {}).get("worker_id"),
        }:
            raise OptimizationError("revalidation must remain within affected station/task scope")
        self.ledger.append("REVALIDATION_DECIDED", {
            "trigger": dict(drift), "affected_scope": dict(drift["scope"]),
            "required_evidence": list(self.manifest.evidence_requirements),
            "evidence_refs": list(evidence_refs), "decision": decision,
        })


def onboard_worker(profile: Any, *, station_id: str, task_class: str, historical_replay_hash: str, shadow_evidence_hash: str, budget: ExplorationBudget) -> dict[str, Any]:
    """Provider-neutral candidate path; caller supplies frozen offline evidence."""
    if not getattr(profile, "id", None) or not getattr(profile, "version", None):
        raise OptimizationError("versioned WorkerProfile required")
    eligible = tuple(str(s) for s in (getattr(profile, "station_eligibility", ()) or ()))
    if eligible and station_id not in eligible:
        raise OptimizationError("worker is outside station eligibility")
    if len(historical_replay_hash) != 64 or len(shadow_evidence_hash) != 64:
        raise OptimizationError("historical replay and shadow evidence hashes required")
    return {
        "worker_id": str(profile.id), "worker_version": profile.version,
        "scope": {"station_id": station_id, "task_class": task_class},
        "stages": ["CANDIDATE_POOL", "HISTORICAL_REPLAY", "SHADOW", "BOUNDED_EXPLORATION", "ELIGIBILITY_DECISION"],
        "historical_replay_hash": historical_replay_hash,
        "shadow_evidence_hash": shadow_evidence_hash,
        "budget": asdict(budget), "production_authority": False,
    }


def pareto_metrics(pairs: Sequence[ShadowPair], *, promoted: int = 0, rolled_back: int = 0, drift_delay_windows: int | None = None) -> dict[str, Any]:
    count = len(pairs)
    if count == 0:
        raise OptimizationError("metrics require paired evidence")
    quality_ok = sum(not (p.baseline.verified and not p.candidate.verified) for p in pairs)
    downshift_ok = sum(p.candidate.verified and Decimal(p.candidate.end_to_end_cost) < Decimal(p.baseline.end_to_end_cost) for p in pairs)
    return {
        "successful_downshift_rate": f"{downshift_ok}/{count}",
        "quality_non_regression": f"{quality_ok}/{count}",
        "cost_delta": str(sum(Decimal(p.candidate.end_to_end_cost) - Decimal(p.baseline.end_to_end_cost) for p in pairs)),
        "latency_delta_ms": sum(p.candidate.latency_ms - p.baseline.latency_ms for p in pairs),
        "worker_selection_regret": "not_estimated_from_shadow_only",
        "recipe_revision_frequency": promoted,
        "drift_detection_delay_windows": drift_delay_windows,
        "rollback_rate": f"{rolled_back}/{promoted}" if promoted else "not_applicable",
    }
