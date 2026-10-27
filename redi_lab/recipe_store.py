"""REDI-08A immutable SQLite recipe store.

The store persists canonical :class:`ProductRecipe` versions without changing
the REDI evidence ledger.  Every recipe row is immutable; lifecycle selection
and rollback are represented by append-only events.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .domain import ProductClass, ProductRecipe, RecipeRung, ValidityCondition
from .domain.serialization import domain_from_json, domain_to_json


RECIPE_STORE_SCHEMA_VERSION = "1"
EXPERIMENTAL_PROMOTION_POLICY_ID = "redi08a_explicit_shadow_enablement_v0"
EXPERIMENTAL_PROMOTION_POLICY_VERSION = 1


class RecipeStoreError(RuntimeError):
    """Base failure for governed recipe-store operations."""


class RecipeConflictError(RecipeStoreError):
    """The same immutable identity was presented with different content."""


class RecipeVersionError(RecipeStoreError):
    """Recipe lineage or version sequencing is invalid."""


class RecipeApprovalError(RecipeStoreError):
    """An explicit governed approval is missing or incomplete."""


class StablePromotionBlocked(RecipeStoreError):
    """REDI-08A deliberately does not promote recipes to stable maturity."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_recipe_hash(recipe: ProductRecipe) -> str:
    return _sha256_text(domain_to_json(recipe))


_SEMANTIC_VOLATILE_KEYS = frozenset({
    "created_at",
    "approved_at",
    "last_revalidated_at",
    "registered_at",
    "recorded_at",
})


def _semantic_payload(value: Any) -> Any:
    """Remove lifecycle fields while retaining the recipe's domain meaning."""

    if isinstance(value, Mapping):
        return {
            str(key): _semantic_payload(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _SEMANTIC_VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_semantic_payload(item) for item in value]
    return value


def recipe_semantic_payload(recipe: ProductRecipe) -> dict[str, Any]:
    raw = json.loads(domain_to_json(recipe))
    payload = _semantic_payload(raw)
    if not isinstance(payload, dict):  # pragma: no cover - serializer guarantee
        raise TypeError("ProductRecipe semantic payload must be an object")
    payload.pop("id", None)
    return payload


def canonical_recipe_semantic_hash(recipe: ProductRecipe) -> str:
    """Stable semantic identity; store content hashes remain backward compatible."""

    return _sha256_text(_canonical_json(recipe_semantic_payload(recipe)))


def product_class_signature_hash(product_class: ProductClass) -> str:
    return _sha256_text(_canonical_json(product_class.signature))


@dataclass(frozen=True)
class ConfidenceMetadata:
    """Descriptive evidence state; intentionally not a probability."""

    tier: str
    evidence_state: str
    source_verified_runs_count: int
    source_failed_runs_count: int
    source_failed_attempts_count: int
    distinct_sibling_products_tested: int
    last_verified_at: str | None
    environment_coverage: tuple[str, ...]
    model_coverage: tuple[str, ...]
    station_evidence_coverage: tuple[str, ...]
    transfer_evidence_count: int

    def __post_init__(self) -> None:
        if self.tier not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("confidence tier must be a descriptive state")
        for name in (
            "source_verified_runs_count",
            "source_failed_runs_count",
            "source_failed_attempts_count",
            "distinct_sibling_products_tested",
            "transfer_evidence_count",
        ):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "evidence_state": self.evidence_state,
            "source_verified_runs_count": self.source_verified_runs_count,
            "source_failed_runs_count": self.source_failed_runs_count,
            "source_failed_attempts_count": self.source_failed_attempts_count,
            "distinct_sibling_products_tested": self.distinct_sibling_products_tested,
            "last_verified_at": self.last_verified_at,
            "environment_coverage": list(self.environment_coverage),
            "model_coverage": list(self.model_coverage),
            "station_evidence_coverage": list(self.station_evidence_coverage),
            "transfer_evidence_count": self.transfer_evidence_count,
            "probability": None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ConfidenceMetadata":
        return cls(
            tier=str(value["tier"]),
            evidence_state=str(value["evidence_state"]),
            source_verified_runs_count=int(value["source_verified_runs_count"]),
            source_failed_runs_count=int(value["source_failed_runs_count"]),
            source_failed_attempts_count=int(value.get("source_failed_attempts_count", 0)),
            distinct_sibling_products_tested=int(value["distinct_sibling_products_tested"]),
            last_verified_at=value.get("last_verified_at"),
            environment_coverage=tuple(str(v) for v in value.get("environment_coverage", ())),
            model_coverage=tuple(str(v) for v in value.get("model_coverage", ())),
            station_evidence_coverage=tuple(
                str(v) for v in value.get("station_evidence_coverage", ())
            ),
            transfer_evidence_count=int(value.get("transfer_evidence_count", 0)),
        )


@dataclass(frozen=True)
class RecipeApproval:
    policy_id: str
    policy_version: int
    approved_by: str
    approval_reference: str
    source_evidence_ids: tuple[str, ...]
    approved_at: datetime

    def __post_init__(self) -> None:
        if not all((self.policy_id, self.approved_by, self.approval_reference)):
            raise RecipeApprovalError("explicit policy, approver, and approval reference are required")
        if int(self.policy_version) < 1 or not self.source_evidence_ids:
            raise RecipeApprovalError("versioned policy and source evidence are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "approved_by": self.approved_by,
            "approval_reference": self.approval_reference,
            "source_evidence_ids": list(self.source_evidence_ids),
            "approved_at": self.approved_at.isoformat(),
            "human_or_audit_approved": True,
        }


@dataclass(frozen=True)
class StoredRecipeVersion:
    recipe: ProductRecipe
    canonical_hash: str
    product_class_signature: Mapping[str, Any]
    product_class_signature_hash: str
    lifecycle_state: RecipeRung
    confidence: ConfidenceMetadata
    provenance: Mapping[str, Any]
    parent_version: int | None
    rollback_target_version: int | None
    revalidation_state: str
    registration_metadata: Mapping[str, Any]
    registered_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS recipe_store_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_versions (
    recipe_id TEXT NOT NULL,
    recipe_version INTEGER NOT NULL,
    canonical_recipe_hash TEXT NOT NULL,
    product_class_id TEXT NOT NULL,
    product_class_signature_json TEXT NOT NULL,
    product_class_signature_hash TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    confidence_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    parent_version INTEGER,
    rollback_target_version INTEGER,
    validity_conditions_json TEXT NOT NULL,
    revalidation_state TEXT NOT NULL,
    recipe_json TEXT NOT NULL,
    registration_metadata_json TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (recipe_id, recipe_version),
    UNIQUE (canonical_recipe_hash)
);

CREATE TABLE IF NOT EXISTS recipe_store_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    recipe_version INTEGER NOT NULL,
    from_version INTEGER,
    to_version INTEGER,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    approval_json TEXT,
    evidence_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (recipe_id, recipe_version)
        REFERENCES recipe_versions(recipe_id, recipe_version)
);
"""


class RecipeStore:
    """Small append-only SQLite repository for immutable recipe versions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO recipe_store_metadata(key,value) VALUES(?,?)",
            ("schema_version", RECIPE_STORE_SCHEMA_VERSION),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "RecipeStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _row_to_version(self, row: sqlite3.Row) -> StoredRecipeVersion:
        recipe = domain_from_json(row["recipe_json"])
        if not isinstance(recipe, ProductRecipe):
            raise RecipeStoreError("stored recipe payload is not ProductRecipe")
        return StoredRecipeVersion(
            recipe=recipe,
            canonical_hash=str(row["canonical_recipe_hash"]),
            product_class_signature=json.loads(row["product_class_signature_json"]),
            product_class_signature_hash=str(row["product_class_signature_hash"]),
            lifecycle_state=RecipeRung(str(row["lifecycle_state"])),
            confidence=ConfidenceMetadata.from_dict(json.loads(row["confidence_json"])),
            provenance=json.loads(row["provenance_json"]),
            parent_version=row["parent_version"],
            rollback_target_version=row["rollback_target_version"],
            revalidation_state=str(row["revalidation_state"]),
            registration_metadata=json.loads(row["registration_metadata_json"]),
            registered_at=str(row["registered_at"]),
        )

    def read(self, recipe_id: str, version: int) -> StoredRecipeVersion:
        row = self._conn.execute(
            "SELECT * FROM recipe_versions WHERE recipe_id=? AND recipe_version=?",
            (recipe_id, int(version)),
        ).fetchone()
        if row is None:
            raise KeyError(f"recipe {recipe_id!r} version {version} not found")
        return self._row_to_version(row)

    def list_versions(self, recipe_id: str) -> tuple[StoredRecipeVersion, ...]:
        rows = self._conn.execute(
            "SELECT * FROM recipe_versions WHERE recipe_id=? ORDER BY recipe_version",
            (recipe_id,),
        ).fetchall()
        return tuple(self._row_to_version(row) for row in rows)

    def list_for_product_class(self, product_class_id: str) -> tuple[StoredRecipeVersion, ...]:
        rows = self._conn.execute(
            "SELECT * FROM recipe_versions WHERE product_class_id=? "
            "ORDER BY recipe_id, recipe_version",
            (product_class_id,),
        ).fetchall()
        return tuple(self._row_to_version(row) for row in rows)

    def _append_event(
        self,
        *,
        event_type: str,
        recipe_id: str,
        recipe_version: int,
        from_version: int | None,
        to_version: int | None,
        policy_id: str,
        policy_version: int,
        approval: Mapping[str, Any] | None,
        evidence: Mapping[str, Any],
        reason: str,
        recorded_at: datetime | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO recipe_store_events(
                event_id,event_type,recipe_id,recipe_version,from_version,to_version,
                policy_id,policy_version,approval_json,evidence_json,reason,recorded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"recipe_store_event_{uuid4().hex}",
                event_type,
                recipe_id,
                int(recipe_version),
                from_version,
                to_version,
                policy_id,
                int(policy_version),
                None if approval is None else _canonical_json(approval),
                _canonical_json(evidence),
                reason,
                (recorded_at or _utcnow()).isoformat(),
            ),
        )

    def register_version(
        self,
        *,
        recipe: ProductRecipe,
        product_class: ProductClass,
        confidence: ConfidenceMetadata,
        provenance: Mapping[str, Any],
        parent_version: int | None,
        rollback_target_version: int | None,
        revalidation_state: str,
        registration_metadata: Mapping[str, Any],
        event_type: str,
        policy_id: str,
        policy_version: int,
        approval: Mapping[str, Any] | None = None,
        event_evidence: Mapping[str, Any] | None = None,
        event_reason: str = "immutable recipe version registered",
    ) -> StoredRecipeVersion:
        if recipe.product_class_id is None or str(recipe.product_class_id) != str(product_class.id):
            raise RecipeVersionError("recipe and ProductClass identities differ")
        if recipe.rung == RecipeRung.VALIDATED:
            raise StablePromotionBlocked("REDI-08A cannot register a stable/validated promotion")
        recipe_json = domain_to_json(recipe)
        recipe_hash = _sha256_text(recipe_json)
        existing = self._conn.execute(
            "SELECT canonical_recipe_hash FROM recipe_versions "
            "WHERE recipe_id=? AND recipe_version=?",
            (str(recipe.id), int(recipe.version)),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != recipe_hash:
                raise RecipeConflictError(
                    "different content under the same recipe identity/version"
                )
            return self.read(str(recipe.id), int(recipe.version))
        versions = self.list_versions(str(recipe.id))
        if versions:
            expected = versions[-1].recipe.version + 1
            if int(recipe.version) != expected or parent_version != versions[-1].recipe.version:
                raise RecipeVersionError(
                    f"new version must be {expected} with parent {versions[-1].recipe.version}"
                )
        elif int(recipe.version) != 1 or parent_version is not None:
            raise RecipeVersionError("first stored recipe version must be version 1 with no parent")
        if rollback_target_version is not None:
            if rollback_target_version not in {v.recipe.version for v in versions}:
                raise RecipeVersionError("rollback target must be an already persisted version")
        registered_at = _utcnow().isoformat()
        signature_json = _canonical_json(product_class.signature)
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(
                """
                INSERT INTO recipe_versions(
                    recipe_id,recipe_version,canonical_recipe_hash,product_class_id,
                    product_class_signature_json,product_class_signature_hash,
                    lifecycle_state,confidence_json,provenance_json,parent_version,
                    rollback_target_version,validity_conditions_json,revalidation_state,
                    recipe_json,registration_metadata_json,registered_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(recipe.id), int(recipe.version), recipe_hash,
                    str(recipe.product_class_id), signature_json, _sha256_text(signature_json),
                    recipe.rung.value, _canonical_json(confidence.to_dict()),
                    _canonical_json(provenance), parent_version, rollback_target_version,
                    _canonical_json([
                        dict(condition)
                        if isinstance(condition, Mapping)
                        else {
                            "description": condition.description,
                            "scope": condition.scope,
                            "checked_via": condition.checked_via,
                            "expires_at": (
                                condition.expires_at.isoformat() if condition.expires_at else None
                            ),
                            "metadata": dict(condition.metadata),
                        }
                        for condition in recipe.validity_conditions
                    ]),
                    revalidation_state, recipe_json,
                    _canonical_json(registration_metadata), registered_at,
                ),
            )
            self._append_event(
                event_type=event_type,
                recipe_id=str(recipe.id),
                recipe_version=int(recipe.version),
                from_version=parent_version,
                to_version=int(recipe.version),
                policy_id=policy_id,
                policy_version=policy_version,
                approval=approval,
                evidence=event_evidence or provenance,
                reason=event_reason,
            )
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return self.read(str(recipe.id), int(recipe.version))

    def enable_experimental(
        self,
        *,
        recipe_id: str,
        source_version: int,
        product_class: ProductClass,
        approval: RecipeApproval,
        confidence: ConfidenceMetadata,
        validity_conditions: Sequence[ValidityCondition],
        worker_preferences: Mapping[Any, Sequence[Any]] | None = None,
        worker_fallbacks: Mapping[Any, Sequence[Any]] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> StoredRecipeVersion:
        source = self.read(recipe_id, source_version)
        if source.lifecycle_state != RecipeRung.CANDIDATE:
            raise RecipeVersionError("experimental enablement must start from a candidate")
        if approval.policy_id != EXPERIMENTAL_PROMOTION_POLICY_ID or (
            approval.policy_version != EXPERIMENTAL_PROMOTION_POLICY_VERSION
        ):
            raise RecipeApprovalError("unexpected experimental enablement policy")
        now = _utcnow()
        child = replace(
            source.recipe,
            version=source_version + 1,
            rung=RecipeRung.SHADOW,
            worker_preferences=(
                source.recipe.worker_preferences
                if worker_preferences is None else dict(worker_preferences)
            ),
            worker_fallbacks=(
                source.recipe.worker_fallbacks
                if worker_fallbacks is None else dict(worker_fallbacks)
            ),
            validity_conditions=tuple(validity_conditions),
            metadata=dict(source.recipe.metadata)
            | dict(metadata or {})
            | {
                "lifecycle_status": "EXPERIMENTAL",
                "promotion_status": "SHADOW_ONLY",
                "stable_or_best_claimed": False,
                "human_or_audit_approved": True,
                "experimental_enablement_policy_id": approval.policy_id,
                "experimental_enablement_policy_version": approval.policy_version,
            },
            created_at=now,
            last_revalidated_at=now,
        )
        provenance = dict(source.provenance) | {
            "parent_recipe_version": source_version,
            "parent_canonical_hash": source.canonical_hash,
            "experimental_approval": approval.to_dict(),
        }
        return self.register_version(
            recipe=child,
            product_class=product_class,
            confidence=confidence,
            provenance=provenance,
            parent_version=source_version,
            rollback_target_version=source_version,
            revalidation_state="VALID_FOR_CONTROLLED_EXPERIMENT",
            registration_metadata={
                "source_registration": dict(source.registration_metadata),
                "activation_scope": "controlled_warm_start_experiment_only",
            },
            event_type="EXPERIMENTAL_VERSION_ENABLED",
            policy_id=approval.policy_id,
            policy_version=approval.policy_version,
            approval=approval.to_dict(),
            event_evidence={
                "source_recipe_version": source_version,
                "source_canonical_hash": source.canonical_hash,
                "source_evidence_ids": list(approval.source_evidence_ids),
            },
            event_reason="explicit human/audit approval for controlled warm-start trials",
        )

    def select_version(
        self,
        *,
        recipe_id: str,
        version: int,
        policy_id: str,
        policy_version: int,
        evidence: Mapping[str, Any],
        reason: str,
    ) -> StoredRecipeVersion:
        stored = self.read(recipe_id, version)
        if stored.lifecycle_state not in {RecipeRung.SHADOW, RecipeRung.CANARY, RecipeRung.VALIDATED}:
            raise RecipeApprovalError("candidate recipe is not warm-start eligible")
        self._append_event(
            event_type="VERSION_SELECTED",
            recipe_id=recipe_id,
            recipe_version=version,
            from_version=self.current_selected_version(recipe_id),
            to_version=version,
            policy_id=policy_id,
            policy_version=policy_version,
            approval=None,
            evidence=evidence,
            reason=reason,
        )
        self._conn.commit()
        return stored

    def rollback(
        self,
        *,
        recipe_id: str,
        target_version: int,
        policy_id: str,
        policy_version: int,
        approved_by: str,
        approval_reference: str,
        reason: str,
    ) -> StoredRecipeVersion:
        current = self.current_selected_version(recipe_id)
        if current is None:
            raise RecipeVersionError("rollback requires a previously selected version")
        target = self.read(recipe_id, target_version)
        if target_version >= current:
            raise RecipeVersionError("rollback target must be an older immutable version")
        self._append_event(
            event_type="ROLLBACK_SELECTED",
            recipe_id=recipe_id,
            recipe_version=target_version,
            from_version=current,
            to_version=target_version,
            policy_id=policy_id,
            policy_version=policy_version,
            approval={
                "approved_by": approved_by,
                "approval_reference": approval_reference,
                "human_or_audit_approved": True,
            },
            evidence={
                "failed_or_newer_version_preserved": current,
                "rollback_target_version": target_version,
                "target_canonical_hash": target.canonical_hash,
            },
            reason=reason,
        )
        self._conn.commit()
        return target

    def record_lookup_decision(
        self,
        *,
        recipe_id: str,
        recipe_version: int,
        compatible: bool,
        policy_id: str,
        policy_version: int,
        evidence: Mapping[str, Any],
        reason: str,
    ) -> None:
        """Persist a deterministic lookup decision without changing selection."""

        self.read(recipe_id, recipe_version)
        self._append_event(
            event_type="LOOKUP_COMPATIBLE" if compatible else "LOOKUP_BLOCKED",
            recipe_id=recipe_id,
            recipe_version=recipe_version,
            from_version=self.current_selected_version(recipe_id),
            to_version=None,
            policy_id=policy_id,
            policy_version=policy_version,
            approval=None,
            evidence=evidence,
            reason=reason,
        )
        self._conn.commit()

    def current_selected_version(self, recipe_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT to_version FROM recipe_store_events "
            "WHERE recipe_id=? AND event_type IN ('VERSION_SELECTED','ROLLBACK_SELECTED') "
            "ORDER BY sequence DESC LIMIT 1",
            (recipe_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def events(self, recipe_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            "SELECT * FROM recipe_store_events WHERE recipe_id=? ORDER BY sequence",
            (recipe_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def promote_to_validated(self, *_args: Any, **_kwargs: Any) -> None:
        raise StablePromotionBlocked(
            "REDI-08A has no stable-promotion path; transfer evidence and a later policy are required"
        )


__all__ = [
    "ConfidenceMetadata",
    "EXPERIMENTAL_PROMOTION_POLICY_ID",
    "EXPERIMENTAL_PROMOTION_POLICY_VERSION",
    "RecipeApproval",
    "RecipeApprovalError",
    "RecipeConflictError",
    "RecipeStore",
    "RecipeStoreError",
    "RecipeVersionError",
    "StablePromotionBlocked",
    "StoredRecipeVersion",
    "canonical_recipe_hash",
    "product_class_signature_hash",
]
