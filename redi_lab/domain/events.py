"""Lifecycle event domain facts (architecture §5.1 normative vocabulary, 18 events).

An event is a provider-independent, immutable domain fact with typed payload.
Payloads are always Mapping[str, Any] at rest; strongly-typed helpers are provided
per-event family so that handlers in Layer C/D (later milestones) can enforce shape
without enforcing implementation details in Layer A.

DO NOT add event execution handlers here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Mapping, Sequence

from .enums import LifecycleEventType
from .identity import SCHEMA_VERSION, EventId, new_id


_PRODUCT_FAMILY: Final[set[LifecycleEventType]] = {
    LifecycleEventType.PRODUCT_ACCEPTED,
    LifecycleEventType.PLAN_COMPILED,
    LifecycleEventType.MILESTONE_COMPLETED,
    LifecycleEventType.INTEGRATION_FAILED,
    LifecycleEventType.PRODUCT_VERIFIED,
}

_NODE_WORKER_FAMILY: Final[set[LifecycleEventType]] = {
    LifecycleEventType.NODE_READY,
    LifecycleEventType.WORKER_STARTED,
    LifecycleEventType.WORKER_COMPLETED,
    LifecycleEventType.GATE_PASSED,
    LifecycleEventType.GATE_FAILED,
    LifecycleEventType.NODE_COMPLETED,
}

_RETRY_ESCALATION_FAMILY: Final[set[LifecycleEventType]] = {
    LifecycleEventType.RETRY_REQUESTED,
    LifecycleEventType.CONTEXT_REBUILD_REQUIRED,
    LifecycleEventType.WORKER_ESCALATION_REQUIRED,
    LifecycleEventType.ARCHITECTURAL_DECISION_REQUIRED,
}

_RECIPE_OPTIMIZATION_FAMILY: Final[set[LifecycleEventType]] = {
    LifecycleEventType.RECIPE_CANDIDATE_CREATED,
    LifecycleEventType.RECIPE_PROMOTED,
    LifecycleEventType.DRIFT_DETECTED,
}

EVENT_FAMILIES: Final[Mapping[str, set[LifecycleEventType]]] = {
    "product": _PRODUCT_FAMILY,
    "node_worker": _NODE_WORKER_FAMILY,
    "retry_escalation": _RETRY_ESCALATION_FAMILY,
    "recipe_optimization": _RECIPE_OPTIMIZATION_FAMILY,
}


def known_event_types() -> Sequence[LifecycleEventType]:
    """Return every normative event in canonical architecture-§5.1 order.

    The ordering below matches architecture.md §5.1 tables top→bottom.
    """
    return [
        LifecycleEventType.PRODUCT_ACCEPTED,
        LifecycleEventType.PLAN_COMPILED,
        LifecycleEventType.MILESTONE_COMPLETED,
        LifecycleEventType.INTEGRATION_FAILED,
        LifecycleEventType.PRODUCT_VERIFIED,
        LifecycleEventType.NODE_READY,
        LifecycleEventType.WORKER_STARTED,
        LifecycleEventType.WORKER_COMPLETED,
        LifecycleEventType.GATE_PASSED,
        LifecycleEventType.GATE_FAILED,
        LifecycleEventType.NODE_COMPLETED,
        LifecycleEventType.RETRY_REQUESTED,
        LifecycleEventType.CONTEXT_REBUILD_REQUIRED,
        LifecycleEventType.WORKER_ESCALATION_REQUIRED,
        LifecycleEventType.ARCHITECTURAL_DECISION_REQUIRED,
        LifecycleEventType.RECIPE_CANDIDATE_CREATED,
        LifecycleEventType.RECIPE_PROMOTED,
        LifecycleEventType.DRIFT_DETECTED,
    ]


@dataclass(frozen=True)
class LifecycleEvent:
    """An immutable domain fact describing one lifecycle transition.

    Attributes:
        id:              opaque event id (unique, provider independent).
        event_type:      normative event type from architecture §5.1.
        timestamp:       UTC wall-clock when the fact was registered.
        schema_version:  contract schema version (SCHEMA_VERSION).
        entity_ids:      related top-level contract IDs (product_id, run_id, ...).
        payload:         controlled provider-independent payload (plain JSON types).
    """

    event_type: LifecycleEventType
    entity_ids: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    id: EventId = field(default_factory=lambda: EventId(new_id("event")))
    timestamp: datetime = field(default_factory=datetime.utcnow)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, LifecycleEventType):
            raise TypeError("event_type must be a LifecycleEventType enum member")
        if not isinstance(self.entity_ids, Mapping):  # pragma: no cover
            raise TypeError("entity_ids must be a Mapping")
        if not isinstance(self.payload, Mapping):  # pragma: no cover
            raise TypeError("payload must be a Mapping")

    @property
    def family(self) -> str:
        for name, members in EVENT_FAMILIES.items():
            if self.event_type in members:
                return name
        raise RuntimeError(  # pragma: no cover - unreachable if enum complete
            f"event_type {self.event_type!r} has no declared family"
        )


__all__ = [
    "EVENT_FAMILIES",
    "LifecycleEvent",
    "known_event_types",
]
