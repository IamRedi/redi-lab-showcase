# PulseBudget forensic repair

Failed run: `wb-a15694f2c50d`. All work is offline; no new owner build or provider request is authorized by this repair.

## Retained facts

The run selected `openai-live-multistation-normal-app-experimental`, not the Professional Product Factory backend. Its evidence contains two completed provider calls, USD 0.0179800, two retained artifacts, and no ProductSpec. The original PulseBudget request was absent from the legacy evidence file, but was recovered read-only from the running Workbench's in-memory snapshot and preserved separately in `build/pulsebudget-repair/owner-snapshot.json`.

The original evidence SHA-256 is `fde4799a5a65469274021c4db19b0bf23a7e323104677a408a1bde90f7eaef1d`. The retained database source SHA-256 is `6b86a3089fffceb11a23c1ab114ba3f5ff1d50692bd8d10db6bd86f78515e455`; the dataflow source SHA-256 is `90bacd6ce3eefd306864be066616a73dbef226bb39dbaf69b4f5c79b9ea044ed`.

The exact failure path is:

1. Workbench receives the PulseBudget owner prompt and the explicitly selected legacy backend.
2. The legacy engine branch failed to invoke its existing scope validator.
3. `normal_app.task_prompt()` supplies fixed `CRM_REQUIREMENT_TEXT` and the Mini CRM interfaces, ignoring the owner prompt.
4. Andinus produces `database.py` for `clients` and `jobs`.
5. Pre-completion `product_integrity._exercise()` opens a clean in-memory database and invokes `database.dashboard_counts(conn)`.
6. SQLite `SUM(CASE WHEN status='NEW' THEN 1 ELSE 0 END)` over an empty `jobs` table returns SQL NULL. The `new_count`, `active_count` and `done_count` aliases are all None; COUNT is zero.
7. Retained `database.py:43`, `int(row['new_count'])`, raises `TypeError: int() argument must be a string, a bytes-like object or a real number, not 'NoneType'`.

This is not a missing transaction amount, missing reference ID, provider truncation, SQLite persistence loss or a defective test fixture. The empty-database test correctly prevented Andinus completion and all six final gates remained NOT_RUN.

The immediate failure is a worker implementation defect in empty aggregate handling, with an insufficiently explicit aggregate contract. The earlier systemic defect is a producer/consumer scope mismatch: an unrelated request was accepted by a fixed CRM factory. Separately, the UI converted the manifest recipe key `mini_crm_job_manager` into a title when no product name was present. There is no evidence that another run's cached state supplied the name.

## Repair boundaries

The legacy branch now validates scope before provider dispatch and persists the original sanitized request even when blocked. It does not silently switch to a different paid backend. Existing Mini CRM briefs remain supported. The aggregate contract requires integer zero counts for empty collections, specifies COUNT/COALESCE normalization, and the existing pre-completion check now identifies the failing interface and empty-aggregate semantics in its error.

The professional runtime compiles declared fields into `product_fields_v2`, retained as `database_contract.json`, hash-bound in task inputs, and supplied to every producer and consumer. Each field has type, required, nullable, default-presence flag, default, validation, serialization and storage representation. Existing v1 fields compile deterministically: required fields are non-nullable; optional fields are nullable. No worker must infer these rules from prose.

Required missing/null/blank values fail before coercion. Omitted insert fields receive declared defaults; omitted update fields preserve existing data. Explicit optional NULL is preserved. Zero and false remain valid values. Date, enum, reference, boolean and integer validation is shared. Unknown input keys fail rather than silently disappearing. Invalid record IDs fail with a validation error before integer conversion.

Money uses SQLite integer minor units at scale 100; API/JSON/UI values are decimal strings in major units. Floating-point money input, nonfinite values, excess precision and SQLite integer overflow are rejected. All money entities use this convention. Direction is an explicit declared enum, never a product-name heuristic. Generic count/sum metrics derive from actual records and have explicit empty identities. Python uses exact decimal arithmetic; browser monetary assertions use BigInt minor units. Typed category/direction filters and declared date/month filters operate on real records.

Andinus integrity now exercises import/schema creation, empty aggregates, representative insert/read/update, required and nullable values, defaults, raw SQLite foreign-key enforcement, exact money storage/round trip, deletion where permitted, close/reopen persistence and idempotent close. Schema initialization is idempotent; incompatible existing column/type/nullability layouts fail closed and require an explicit migration. It never silently rewrites an existing schema.

The finance regression also exposed boolean form-case serialization and the acceptance harness's text-only edit assumption. Boolean form input now normalizes case, and the browser journey exercises edits of numeric/date/boolean fields as well as text. Frozen final gate names, order and pass/fail requirements remain unchanged.

## Identity authority

An explicit owner `called`/`named` name becomes the canonical ProductSpec identity before planning. Raw provider specification output remains immutable and separate. That canonical spec flows to worker prompts, run metadata, package metadata, README and the trusted generated UI title. Workbench reads the current build's canonical spec first, then recorded metadata/request; it no longer converts a recipe key to a title. A verified historical Mini CRM package may supply a legacy package identity only if no valid product name exists. Historical failed runs are not renamed on disk.

For the failed PulseBudget run, a restarted process cannot recover an original prompt that the old implementation never persisted. The forensic snapshot preserves that recovered fact outside the immutable run. The offline UI fixture uses this captured snapshot explicitly; it does not claim a PulseBudget ProductSpec existed historically.

## Offline proof

`tests/fixtures/pulsebudget_retained_failure.json` contains hash-checked retained artifact copies and the recovered request. Tests reproduce the original TypeError, prove clearer fail-closed rejection, and prove the original owner prompt is blocked on the wrong backend with no provider dispatch.

CRM/workflow, finance and non-relational utility specs use the same database contract and integrity function. The finance spec is manually authored from the described shape for offline testing, not labeled as a retained/generated PulseBudget spec. Its fake-provider factory run exercises real SQLite, real Chromium, all six final gates, packaging, restart persistence and reviewer checks. Product-name regression traverses PulseBudget, PitlaneOne Lite and Mini CRM in both orders.

Verification results, original traceback, preservation hashes and browser screenshots belong in `build/pulsebudget-repair/`. A new live run remains a separate authorization and must use the Professional Product Factory backend in a fresh process; this repair performs none.
