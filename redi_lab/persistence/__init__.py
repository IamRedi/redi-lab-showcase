"""Persistence layer (Layer C) for REDI Lab evidence ledger.

Exports public interfaces: EvidenceLedger, repositories, schema bootstrap,
exceptions, integrity helpers, and environment capture.

All imports flow from persistence → redi_lab.domain. Reverse imports (domain →
persistence) are explicitly forbidden by architectural layering.
"""

from .exceptions import (
    AlreadyFrozenError,
    ClosedRunError,
    DuplicateLedgerEntryError,
    IncompleteEvidenceError,
    MalformedPayloadError,
    NotFrozenError,
    UnsupportedPersistenceSchemaError,
)
from .schema import SCHEMA_VERSION_CURRENT, bootstrap_schema, check_schema_version
from .integrity import canonical_hash, payload_hash_from_json, verify_chain
from .ledger import AppendRequest, EvidenceLedger
from .repositories import ExperimentRepository, FactoryRunRepository
from .env_capture import capture_repro_environment, insert_pricing_evidence, make_pricing_evidence_row

__all__ = [
    "AlreadyFrozenError",
    "ClosedRunError",
    "DuplicateLedgerEntryError",
    "IncompleteEvidenceError",
    "MalformedPayloadError",
    "NotFrozenError",
    "UnsupportedPersistenceSchemaError",
    "SCHEMA_VERSION_CURRENT",
    "bootstrap_schema",
    "check_schema_version",
    "canonical_hash",
    "payload_hash_from_json",
    "verify_chain",
    "AppendRequest",
    "EvidenceLedger",
    "ExperimentRepository",
    "FactoryRunRepository",
    "capture_repro_environment",
    "insert_pricing_evidence",
    "make_pricing_evidence_row",
]
