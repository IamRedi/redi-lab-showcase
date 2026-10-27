"""Deterministic integrity helpers for REDI evidence ledger.

Reuses REDI-01 canonical serializer only. Never invents a second serializer.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..domain import domain_to_json


def canonical_hash(entity: Any) -> str:
    """sha256(domain_to_json(entity).encode('utf-8')).hexdigest()

    Deterministic — same entity object semantics → same hash.
    """
    payload = domain_to_json(entity)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def payload_hash_from_json(payload_json: str) -> str:
    """Hash stored JSON string directly (used to detect JSON tampering)."""
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def compute_prev_hash(prev_seq: int, prev_payload_sha256: str) -> str:
    """Compute prev_hash value for current row: sha256(prev_seq:prev_payload_sha256)."""
    preimage = f"{prev_seq}:{prev_payload_sha256}"
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def verify_chain(
    prev_seq: int | None,
    prev_payload_sha256: str | None,
    cur_seq: int,
    cur_payload_sha256: str,
    cur_prev_hash: str | None,
) -> bool:
    """Verify cur_prev_hash correctly chains from (prev_seq, prev_hash).

    First-entry (prev_seq==None and prev_payload_sha256==None) → valid iff cur_prev_hash is None.
    """
    if prev_seq is None and prev_payload_sha256 is None:
        return cur_prev_hash is None
    if prev_seq is None or prev_payload_sha256 is None:
        return False
    if cur_prev_hash is None:
        return False
    expected = compute_prev_hash(prev_seq, prev_payload_sha256)
    return expected == cur_prev_hash


__all__ = [
    "canonical_hash",
    "compute_prev_hash",
    "payload_hash_from_json",
    "verify_chain",
]
