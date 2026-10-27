"""Deterministic worker selection for station pools (REDI-04A, policy V0).

Pure Layer C.  NO learned router, NO bandit, NO optimizer, NO scoring —
selection is fully explained by explicit, ordered rules applied to the
station's WorkerPool:

  1. station eligibility   — profile.station_eligibility, when non-empty,
                             must contain the station id;
  2. availability          — availability_status must be None/"available";
  3. task compatibility    — when the station declares applicable_task_classes
                             and the task carries metadata["task_class"], the
                             task class must be listed;
  4. runtime binding       — a runner must be bound for the worker id;
  5. fallback exclusions   — workers in ``exclude_worker_ids`` are skipped
                             (they failed on a previous attempt);
  6. configured preference — lowest PoolEntry.preference_rank wins; ties are
                             broken by profile id (lexicographic) so the same
                             inputs always select the same worker.

The returned :class:`WorkerSelection` carries the machine-readable reason
trace (policy id/version, skipped candidates with reasons, selection kind)
that the controller persists into RunEvidence metadata.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .domain import StationContract, StationId, TaskContract, WorkerId, WorkerPool, WorkerPoolId, WorkerProfile

SELECTION_POLICY_ID = "deterministic_v0"
SELECTION_POLICY_VERSION = 1

SELECTION_KIND_INITIAL = "initial"
SELECTION_KIND_SAME_WORKER_RETRY = "same_worker_retry"
SELECTION_KIND_WORKER_FALLBACK = "worker_fallback"


@dataclass(frozen=True)
class WorkerSelection:
    """Outcome of one deterministic selection, with its full reason trace."""

    profile: WorkerProfile
    runner: Any
    policy_id: str
    policy_version: int
    selection_kind: str
    reason: str
    preference_rank: int
    skipped: tuple[dict[str, str], ...] = field(default_factory=tuple)
    previous_worker_id: str | None = None

    def to_evidence(self) -> dict[str, Any]:
        """Canonical JSON-primitive evidence dict for RunEvidence.metadata."""
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "selection_kind": self.selection_kind,
            "reason": self.reason,
            "worker_id": str(self.profile.id),
            "worker_display_name": self.profile.display_name,
            "worker_profile_version": self.profile.version,
            "worker_provider_ref": self.profile.provider_opaque_reference or "",
            "preference_rank": self.preference_rank,
            "previous_worker_id": self.previous_worker_id,
            "skipped": [dict(s) for s in self.skipped],
        }


def _skip_reasons(
    profile: WorkerProfile,
    *,
    station: StationContract,
    task: TaskContract,
    runner_missing: bool,
    excluded: bool,
) -> str | None:
    """Return why this profile is not selectable now, or None if eligible.

    Evaluation order is fixed and documented in the module docstring.
    """
    if excluded:
        return "excluded: failed on a previous attempt of this station"
    status = profile.availability_status
    if status is not None and status != "available":
        return f"unavailable: availability_status={status}"
    eligibility = tuple(profile.station_eligibility or ())
    if eligibility and str(station.id) not in {str(s) for s in eligibility}:
        return "not eligible for this station"
    applicable = tuple(station.applicable_task_classes or ())
    task_class = (task.metadata or {}).get("task_class")
    if applicable and task_class is not None and str(task_class) not in applicable:
        return f"task class mismatch: {task_class!r} not in {list(applicable)}"
    if runner_missing:
        return "no runner bound for this worker profile"
    return None


def resolve_worker(
    *,
    pool: WorkerPool,
    runners: Mapping[str, Any],
    station: StationContract,
    task: TaskContract,
    exclude_worker_ids: Sequence[str] = (),
    attempt_index: int = 0,
    previous_worker_id: str | None = None,
) -> WorkerSelection | None:
    """Select the next eligible worker from ``pool`` deterministically.

    Returns None when no eligible worker remains (pool exhaustion for this
    station/task under the current exclusions).  Same inputs (pool, runners,
    station, task, exclusions) always yield the same selection.
    """
    excluded = {str(w) for w in exclude_worker_ids}
    ordered = sorted(
        pool.entries,
        key=lambda e: (int(e.preference_rank), str(e.profile.id)),
    )
    skipped: list[dict[str, str]] = []
    for entry in ordered:
        profile = entry.profile
        wid = str(profile.id)
        reason = _skip_reasons(
            profile,
            station=station,
            task=task,
            runner_missing=wid not in runners,
            excluded=wid in excluded,
        )
        if reason is not None:
            skipped.append({"worker_id": wid, "reason": reason})
            continue
        if attempt_index == 0:
            kind = SELECTION_KIND_INITIAL
            why = f"highest-ranked eligible worker (rank {int(entry.preference_rank)})"
        elif previous_worker_id is not None and wid == str(previous_worker_id):
            kind = SELECTION_KIND_SAME_WORKER_RETRY
            why = "no alternative eligible worker remains; retrying same worker"
        else:
            kind = SELECTION_KIND_WORKER_FALLBACK
            why = "prior worker failed deterministic verification; next eligible worker selected"
        if skipped:
            why += "; skipped: " + "; ".join(f"{s['worker_id']} ({s['reason']})" for s in skipped)
        return WorkerSelection(
            profile=profile,
            runner=runners[wid],
            policy_id=str(pool.selection_policy_id or SELECTION_POLICY_ID),
            policy_version=SELECTION_POLICY_VERSION,
            selection_kind=kind,
            reason=why,
            preference_rank=int(entry.preference_rank),
            skipped=tuple(skipped),
            previous_worker_id=previous_worker_id if attempt_index > 0 else None,
        )
    return None


__all__ = [
    "SELECTION_KIND_INITIAL",
    "SELECTION_KIND_SAME_WORKER_RETRY",
    "SELECTION_KIND_WORKER_FALLBACK",
    "SELECTION_POLICY_ID",
    "SELECTION_POLICY_VERSION",
    "WorkerSelection",
    "pool_from_snapshot",
    "pool_snapshot",
    "pool_snapshot_hash",
    "resolve_worker",
]


# ---------------------------------------------------------------------------
# REDI-04B — pool provenance: deterministic snapshot + hash + reconstruction.
#
# One canonical snapshot per station/run (persisted with PLAN_COMPILED),
# lightweight hash references per attempt (RunEvidence.metadata), and
# reconstruction for crash/resume so historical run semantics win over later
# configuration changes.  All values are JSON primitives; no Python repr.
# ---------------------------------------------------------------------------


def pool_snapshot(*, pool: WorkerPool, station: StationContract, task: TaskContract) -> dict[str, Any]:
    """Deterministic provider-neutral snapshot of a pool's selection config.

    Captures exactly the state that drives V0 selection: ordered candidates
    (rank, worker id), profile versions, availability, station eligibility,
    the station's applicable task classes, and the task class under test.
    Candidate order is the selection order (preference_rank, worker id).
    """
    task_class = (task.metadata or {}).get("task_class")
    return {
        "pool_id": str(pool.id),
        "station_id": str(pool.station_id if pool.station_id is not None else station.id),
        "station_role": str(pool.role or getattr(station, "role", "")),
        "selection_policy_id": str(pool.selection_policy_id or SELECTION_POLICY_ID),
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "station_applicable_task_classes": [str(x) for x in (station.applicable_task_classes or ())],
        "task_class": None if task_class is None else str(task_class),
        "candidates": [
            {
                "worker_id": str(entry.profile.id),
                "display_name": str(entry.profile.display_name or ""),
                "profile_version": str(entry.profile.version or ""),
                "preference_rank": int(entry.preference_rank),
                "availability_status": (
                    None if entry.profile.availability_status is None else str(entry.profile.availability_status)
                ),
                "station_eligibility": [str(s) for s in (entry.profile.station_eligibility or ())],
                "provider_opaque_reference": str(entry.profile.provider_opaque_reference or ""),
            }
            for entry in sorted(
                pool.entries,
                key=lambda e: (int(e.preference_rank), str(e.profile.id)),
            )
        ],
    }


def pool_snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON encoding of a pool snapshot."""
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def pool_from_snapshot(snapshot: Mapping[str, Any]) -> WorkerPool:
    """Reconstruct a WorkerPool from its persisted snapshot.

    Rebuilt pools reproduce the original selection semantics (ids, ranks,
    versions, availability, eligibility, policy).  Runtime runner bindings
    are supplied separately by the caller.
    """
    entries = []
    for cand in snapshot.get("candidates") or []:
        profile = WorkerProfile(
            id=WorkerId(cand["worker_id"]),
            display_name=str(cand.get("display_name") or ""),
            version=str(cand.get("profile_version") or ""),
            availability_status=cand.get("availability_status"),
            station_eligibility=[StationId(s) for s in (cand.get("station_eligibility") or [])],
            provider_opaque_reference=cand.get("provider_opaque_reference") or None,
        )
        entries.append(_entry(profile, int(cand.get("preference_rank", 0))))
    return WorkerPool(
        id=WorkerPoolId(str(snapshot["pool_id"])),
        station_id=StationId(str(snapshot["station_id"])) if snapshot.get("station_id") else None,
        role=str(snapshot.get("station_role") or ""),
        entries=entries,
        selection_policy_id=str(snapshot.get("selection_policy_id") or SELECTION_POLICY_ID),
    )


def _entry(profile: WorkerProfile, rank: int):
    from .domain import PoolEntry

    return PoolEntry(profile=profile, preference_rank=rank)
