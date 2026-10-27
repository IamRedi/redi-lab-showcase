"""Custom exception taxonomy for REDI-02 persistence/evidence/replay layers.

All exceptions are simple typed subclasses of builtin ValueError or
RuntimeError with no extra attributes unless specified.
"""


class UnsupportedPersistenceSchemaError(ValueError):
    """Raised when persistence schema version does not match supported version."""


class DuplicateLedgerEntryError(ValueError):
    """Raised when append violates entity_id or payload_sha256 unique constraint."""


class AlreadyFrozenError(RuntimeError):
    """Raised attempting to freeze already-frozen ExperimentManifest."""


class NotFrozenError(RuntimeError):
    """Raised attempting to amend unfrozen ExperimentManifest."""


class ClosedRunError(RuntimeError):
    """Raised attempting close already-closed FactoryRun."""


class IncompleteEvidenceError(ValueError):
    """Raised replay references missing experiment/run/entity rows."""


class MalformedPayloadError(ValueError):
    """Raised stored payload fails deserialization or decode failures."""


__all__ = [
    "UnsupportedPersistenceSchemaError",
    "DuplicateLedgerEntryError",
    "AlreadyFrozenError",
    "NotFrozenError",
    "ClosedRunError",
    "IncompleteEvidenceError",
    "MalformedPayloadError",
]
