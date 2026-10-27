# REDI Evidence Ledger

## Purpose
The REDI Evidence Ledger is REDI Lab's minimum trustworthy local append-only record
of every research artifact needed to (a) prove exactly what a Factory Run did,
(b) reconstruct the final state without re-running any models or workers, and
(c) defend the domain model from accidental or deliberate silent mutation of
historical evidence.

It is the measuring instrument for REDI Lab research. It does not call models,
execute workers, orchestrate agents, route tasks, or implement any REDI-03
capability.

## Storage Architecture
- Persistence / evidence / replay packages sit above the pure domain layer. The
  dependency direction is strictly `persistence → domain`, `evidence → domain`,
  `replay → domain`. The domain never imports persistence or runtime modules.
- A single local SQLite file (stdlib `sqlite3` only, no ORM, no network).
- Public interfaces (small, concrete, no generic `BaseRepository`):
  - `EvidenceLedger` — append/query the immutable evidence stream
  - `ExperimentRepository` — store/freeze/amend ExperimentManifests with revision lineage
  - `FactoryRunRepository` — register / close Factory Runs
  - `ReplayReader` — deterministic, read-only, reconstruct recorded state from
    the ledger plus two repository tables
- Raw SQL lives in only three locations: `schema.py` (DDL + bootstrap),
  `ledger.py` and `repositories.py` (DML only).

## Append-only semantics
- Public APIs expose: `append`, `append_batch`, `query_*`, `replay`.
- The public surface intentionally contains no `update_*`, `delete_*`, `remove_*`
  methods.
- Two explicit, audited mutations only are allowed:
  1. `ExperimentRepository.freeze()` sets `frozen_ts` on a manifest once
     (column was NULL).
  2. `FactoryRunRepository.close_run()` sets `closed_at` and the final columns
     on a run once (column was NULL).
- Everything else is INSERT-only; corrections become new rows in
  `evidence_entries` with `correction_of_entry_id` set, leaving the original
  intact.
- `evidence_entries.global_sequence` is monotonic +1 so ledger order is
  deterministic.

## DB Schema / Version
Five tables:

1. `persistence_schema_metadata(key PRIMARY KEY, value)` — contains
   `schema_version = '1'`.
2. `experiment_manifests` — preregistered experiments, with `frozen_ts` (single-set),
   `latest_rev_of_id` FK for amendment lineage.
3. `factory_runs` — runs with FK to experiment and final columns set by `close_run`.
4. `evidence_entries` — append-only fact table (global_sequence UNIQUE, entity_id
   UNIQUE, payload_sha256 UNIQUE, prev_hash, correction_of_entry_id FK,
   factory_run_id / experiment_id FKs, CHECK restricts entity_type to the 6
   allowed values.
5. `pricing_evidence` — Decimal-accurate pricing rows captured at the time of evidence
   recording (immutable INSERTs only).

Persistence schema version strategy: single row `schema_version`; current = `'1'`.
Any other value → `UnsupportedPersistenceSchemaError` on open. No migration
framework yet; unsupported future versions are rejected explicitly.

## Duplicate / Idempotency Policy
Chosen policy: **explicit rejection, not idempotent retry**.

- `evidence_entries.entity_id` UNIQUE + `evidence_entries.payload_sha256` UNIQUE.
- Re-inserting the same entity_id or same serialized payload raises
  `DuplicateLedgerEntryError` with no side effects.
- Callers must not blindly retry: distinct recording is distinct fact, idempotent
  retry (insert-on-success, return existing) is explicitly deferred.

## Integrity / Hash Strategy
For every ledger entry:
1. Serialize payload with the canonical `domain_to_json()` (sort_keys, Decimal→str,
   datetime→iso8601-utc, StrEnum→value, `__type__` tag).
2. Compute `payload_sha256 = sha256(payload_json.encode("utf-8")).hexdigest()`.
3. Compute `prev_hash = sha256(f"{prev_global_sequence}:{prev_payload_sha256}")`
   — first entry in the entire ledger has `prev_hash = NULL`.

No blockchain claims are made. The chain is used to detect accidental corruption
or accidental reordering, and payload hashes detect byte-level tampering. The chain
integrity chain covers exactly this definition and nothing more.

## Experiment Manifest Freeze / Amendments
- `store_preregistered(m)` → inserted, frozen_ts NULL.
- `freeze(id)` → sets `frozen_ts` exactly once. Second `frozen already
  frozen_ts` raises `AlreadyFrozenError`.
- `amend(id, new_m)` → only allowed on a frozen manifest. Inserts a new row
  with `latest_rev_of_id → original.id`; original row is never modified;
  `list_revisions(original_id)` returns the original + amendments in order;
  `read(original_id)` still returns the original row unchanged.

## Transaction Semantics
- `EvidenceLedger.append` — single INSERT then one COMMIT.
- `EvidenceLedger.append_batch(items)` → BEGIN; repeated `_append_insert` for each
  item; COMMIT only after every insert; if any insert raises → ROLLBACK, no partial
  rows.
- `store_preregistered` / `amend` / `close_run` each commit after one logical
  operation per call; one transactional batch so either the repository operations are
  transactionally (evidence ledger append with rollback proof via a failed rollback test
  `test_transaction_rollback_batch` proves failed batch leaves no partial state.

## Replay Semantics
Deterministic, read-only reconstruction. Algorithm:
1. Open the database `file:<path>?mode=ro" URI (sqlite in SQLite read-only mode
   when given a Path/str; external calls, or reuse an existing Connection.
2. SELECT factory_runs row; if missing → IncompleteEvidenceError("not found");
3. SELECT experiment row (if referenced but missing → IncompleteEvidenceError("missing").
4. SELECT evidence_entries WHERE factory_run_id = ? ORDER BY global_sequence ASC.
5. For each row:
   - recalculate payload_sha256; mismatch → integrity_flags TAMPERED_PAYLOAD_SHA_MISMATCH.
   - verify prev_hash chain vs. prev entry's (seq, sha) pair; broken → BROKEN_PREV_HASH_CHAIN.
   - try decode payload_json decode failure (any Exception) → MALFORMED_PAYLOAD_DECODE.
6. Classify each decoded object by its Python class → ordered_events,
   run_evidences, failures, gate_results.
7. Derive only, state strictly from rows and not inferred fields:
   - observed_run_status = closed_at != NULL ? factory_runs.final_status : pending.
   - completed_task_ids = gate_results WHERE status == PASS AND target_task_id exists.
   - gate_outcomes = per-gate (id, status, target_task_id).
   - final_verification_state = factory_runs.verified=1 AND PRODUCT_VERIFIED event present.
8. Integrity precedence: TAMPERED if any sha mismatch; else INCOMPLETE_CHAIN if any
   chain/malformed/decode; else OK.
9. Zero model calls, zero workers, zero writes to the database, zero network. Repeated
   replay over an unchanged ledger produces semantically equivalent output.

## Privacy Boundaries
Environment capture records only: Python version, OS/platform string, optional repository
commit SHA, REDI schema version, optional dependency_ref, timestamp. Any metadata
captures defensive blocklist: HOME/USER/USERNAME/KEY/TOKEN/SECRET/PASSWORD/
CREDENTIAL/APIKEY substrings reject any caller-provided `extra` metadata keys containing
blocklisted substrings raise ValueError with a "blocklist" message. No env
var dumps, no username/home path values, no API keys, no secrets in payloads.

Pricing stored as opaque provider/model refs, Decimal amounts never hard-coded “truths”.

## Deferred Functionality (not in REDI-02, for later milestones)
- Distributed / remote storage, multi-process locks,
- Migration framework for schema versions other than '1'
- Idempotent retry duplicate policy variant (we chose explicit rejection today
- Compaction / retention window enforcement (data_retention_expired columns are
  present but never enforced)
- Event handlers / orchestration runtime / worker execution
- Model calls, routing, scheduling recipe learning, optimizer, Docker/Temporal, API/UI/CLI
(Replay LLM inference re-execution
