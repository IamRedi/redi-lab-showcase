"""SQLite schema bootstrap + version check for REDI evidence ledger.

Five tables per REDI-02 spec §8.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .exceptions import UnsupportedPersistenceSchemaError

SCHEMA_VERSION_CURRENT: str = "1"

SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS persistence_schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_manifests (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    hypothesis_reference TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    frozen_ts TEXT,
    latest_rev_of_id TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL UNIQUE,
    preregistered_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data_retention_expired INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(latest_rev_of_id) REFERENCES experiment_manifests(id)
);

CREATE TABLE IF NOT EXISTS factory_runs (
    id TEXT PRIMARY KEY,
    product_spec_id TEXT,
    dag_id TEXT,
    recipe_id TEXT,
    experiment_id TEXT,
    mode TEXT NOT NULL,
    final_status TEXT NOT NULL DEFAULT 'pending',
    verified INTEGER NOT NULL DEFAULT 0,
    timing_json TEXT,
    total_cost_json TEXT,
    total_tokens_json TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    data_retention_expired INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(experiment_id) REFERENCES experiment_manifests(id)
);
CREATE INDEX IF NOT EXISTS idx_factory_runs_experiment ON factory_runs(experiment_id);

CREATE TABLE IF NOT EXISTS evidence_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    global_sequence INTEGER UNIQUE NOT NULL,
    entity_id TEXT UNIQUE NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN (
        'RunEvidence','LifecycleEvent','FailureRecord','GateResult',
        'EnvironmentInfo','PricingEvidence'
    )),
    factory_run_id TEXT,
    experiment_id TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL UNIQUE,
    prev_hash TEXT,
    correction_of_entry_id INTEGER,
    recorded_at TEXT NOT NULL,
    data_retention_expired INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(factory_run_id) REFERENCES factory_runs(id),
    FOREIGN KEY(experiment_id) REFERENCES experiment_manifests(id),
    FOREIGN KEY(correction_of_entry_id) REFERENCES evidence_entries(entry_id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_entries_run ON evidence_entries(factory_run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_entries_experiment ON evidence_entries(experiment_id);
CREATE INDEX IF NOT EXISTS idx_evidence_entries_type ON evidence_entries(entity_type);

CREATE TABLE IF NOT EXISTS pricing_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currency TEXT NOT NULL,
    amount_decimal_str TEXT NOT NULL,
    pricing_schedule_version TEXT,
    effective_dt_iso TEXT,
    source_ref TEXT NOT NULL,
    opaque_provider_ref TEXT,
    captured_at TEXT NOT NULL,
    data_retention_expired INTEGER NOT NULL DEFAULT 0
);
"""


def bootstrap_schema(conn: sqlite3.Connection) -> None:
    """Apply PRAGMA + execute schema.

    Idempotent: CREATE TABLE IF NOT EXISTS everywhere; inserts schema_version
    row only if absent.
    """
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_SQL)
    cur = conn.execute(
        "SELECT value FROM persistence_schema_metadata WHERE key = 'schema_version'"
    )
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO persistence_schema_metadata (key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION_CURRENT),
        )
        conn.commit()


def check_schema_version(conn: sqlite3.Connection) -> str:
    """Return schema_version string; raise if unsupported or missing.

    Raises UnsupportedPersistenceSchemaError for != SCHEMA_VERSION_CURRENT.
    """
    cur = conn.execute(
        "SELECT value FROM persistence_schema_metadata WHERE key = 'schema_version'"
    )
    row = cur.fetchone()
    if row is None:
        raise UnsupportedPersistenceSchemaError(
            "persistence_schema_metadata row missing: run bootstrap_schema first"
        )
    value = row[0]
    if value != SCHEMA_VERSION_CURRENT:
        raise UnsupportedPersistenceSchemaError(
            f"Unsupported persistence schema version {value!r}; expected {SCHEMA_VERSION_CURRENT!r}"
        )
    return value


__all__ = [
    "SCHEMA_SQL",
    "SCHEMA_VERSION_CURRENT",
    "bootstrap_schema",
    "check_schema_version",
]
