"""Stable identity semantics and schema versioning for all top-level contracts.

Identity rules:
- Every top-level persisted/exchanged contract owns a unique opaque `*Id` value.
- IDs are provider-independent and carry no embedded provider/model/path names.
- `VersionedContract` is the common top-level base carrying `schema_version`.
- Incompatible schema versions raise `SchemaVersionError` at decode / validation time.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import (
    ClassVar,
    Generic,
    NewType,
    Protocol,
    TypeVar,
    runtime_checkable,
)

from .enums import SchemaVersion

# Only one initial version required by REDI-01.
SCHEMA_VERSION: int = SchemaVersion.V1.value

_ALNUM = re.compile(r"^[A-Za-z0-9._:\-]{4,256}$")


def _validate_opaque_id(value: str) -> None:
    """Provider-independent IDs are stable ASCII printable strings.

    No slashes, no spaces, no '@<provider>', no absolute paths.
    """
    if not isinstance(value, str):  # pragma: no cover - type safety
        raise ValueError(f"ID must be a string, got {type(value).__name__}")
    if not _ALNUM.match(value):
        raise ValueError(
            "ID must contain only ASCII letters, digits, and [._:-], "
            f"length 4..256; got {value!r}"
        )


# --- Opaque NewType ids -----------------------------------------------------

ProductId = NewType("ProductId", str)
ProductClassId = NewType("ProductClassId", str)
RecipeId = NewType("RecipeId", str)
FactoryRunId = NewType("FactoryRunId", str)
ExperimentId = NewType("ExperimentId", str)
TaskId = NewType("TaskId", str)
DagNodeId = NewType("DagNodeId", str)
StationId = NewType("StationId", str)
WorkerId = NewType("WorkerId", str)
WorkerPoolId = NewType("WorkerPoolId", str)
ContextManifestId = NewType("ContextManifestId", str)
HandoffManifestId = NewType("HandoffManifestId", str)
GateResultId = NewType("GateResultId", str)
EvidenceId = NewType("EvidenceId", str)
FailureId = NewType("FailureId", str)
RunId = NewType("RunId", str)
EventId = NewType("EventId", str)

_CONTRACT_ID_TAG = {
    "product": ProductId,
    "product_class": ProductClassId,
    "recipe": RecipeId,
    "factory_run": FactoryRunId,
    "experiment": ExperimentId,
    "task": TaskId,
    "dag_node": DagNodeId,
    "station": StationId,
    "worker": WorkerId,
    "worker_pool": WorkerPoolId,
    "context_manifest": ContextManifestId,
    "handoff_manifest": HandoffManifestId,
    "gate_result": GateResultId,
    "evidence": EvidenceId,
    "failure": FailureId,
    "run": RunId,
    "event": EventId,
}


def new_id(prefix: str) -> str:
    """Generate a new stable provider-independent opaque id.

    Format:  ``<prefix>_<uuid4-hex>`` — never encodes environment names or
    provider details.
    """
    if prefix not in _CONTRACT_ID_TAG:
        raise ValueError(f"Unknown ID prefix: {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex}"


T = TypeVar("T", bound="VersionedContract")


class SchemaVersionError(ValueError):
    """Raised when a contract's schema version is unsupported or unknown."""


@runtime_checkable
class VersionedContract(Protocol):
    """Structural protocol satisfied by every top-level dataclass contract.

    All top-level contracts must declare:
      - ``id`` (opaque NewType id)
      - ``schema_version`` (int, class-defaulted to SCHEMA_VERSION)
    """

    id: object  # exact type is one of the NewType ids above
    schema_version: int
    SCHEMA_VERSION_CONSTANT: ClassVar[int] = SCHEMA_VERSION


@dataclass(frozen=True)
class ContractId(Generic[T]):
    """Strongly typed wrapper pairing a tag with an opaque id value.

    Primarily useful in registry signatures that want compile-time tag check.
    Most contracts keep the raw NewType id as their ``id`` field instead.
    """

    tag: str
    value: str = field(default_factory=lambda: new_id("contract"))

    def __post_init__(self) -> None:
        if self.tag not in _CONTRACT_ID_TAG:
            raise ValueError(f"Unknown ID tag: {self.tag!r}")
        _validate_opaque_id(self.value)

    def __str__(self) -> str:  # pragma: no cover - debug
        return f"{self.tag}:{self.value}"


__all__ = [
    "SCHEMA_VERSION",
    "ProductId",
    "ProductClassId",
    "RecipeId",
    "FactoryRunId",
    "ExperimentId",
    "TaskId",
    "DagNodeId",
    "StationId",
    "WorkerId",
    "WorkerPoolId",
    "ContextManifestId",
    "HandoffManifestId",
    "GateResultId",
    "EvidenceId",
    "FailureId",
    "RunId",
    "EventId",
    "new_id",
    "SchemaVersionError",
    "VersionedContract",
    "ContractId",
]
