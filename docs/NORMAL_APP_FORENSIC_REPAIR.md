# Normal-app forensic repair: wb-847df3c91090

## Historical facts

Source: `build/workbench-builds/wb-847df3c91090/live_run_evidence.json` and **every retained artifact** under its `gates/mini_crm` directory, including the service bytecode inventory. The originals were hashed before reproduction and were not edited or executed in place. Byte-identical source/evidence copies are checked in under `tests/fixtures/mini_crm_wb847df3c91090`.

All five tasks were recorded **COMPLETED**; the provider failure map was empty. Gentius successfully returned and retained `service.py`; that task status did not mean the application could import or pass acceptance.

| Task / persona | Input | Cached input | Output | USD | Finish reason |
|---|---:|---:|---:|---:|---|
| build_database / Andinus | 202 | 0 | 1811 | 0.022136 | stop |
| build_dataflow / Bindus | 150 | 0 | 543 | 0.006816 | stop |
| build_core / Agron | 1203 | 0 | 2502 | 0.032430 | stop |
| build_ui / Teuta | 1809 | 0 | 2571 | 0.034470 | stop |
| integration_assembly / Gentius | 2452 | 0 | 2869 | 0.039332 | stop |
| Total | 5816 | 0 | 10296 | **0.135184** | 5 calls |

Provider/model: OpenAI / `gpt-5.6-terra`; pricing state `SNAPSHOT_PROVEN`. The owner reported a $1.00 ceiling. The historical JSON does not contain that ceiling, request bodies, reasoning-token breakdown, original prompts, or the running server's source revision; these cannot be reconstructed as persisted facts. The input/output figures above are the recorded aggregate token categories, not visible-content-only tokens.

Artifact SHA-256 values match the task completion events:

| Artifact | SHA-256 |
|---|---|
| database.py | `f6bda9958ecf0b5d4e91b86ec1d45254e3333540ef8d45c6cfff23722071decb` |
| dataflow.py | `78ebabf8b9a598a44a5ca4f3873a237a9b6bb088c15e39e7e938075e69b8126e` |
| core.py | `58dee84f2998df91d71dd4895d33da13b5e04f3d4560992c4f0bae515a273dde` |
| ui.py | `6731e07c65e0aef1b47260222de1d91c94ef0cf0128dd1eabbcb7c16dfceec35` |
| service.py | `362515e8bfe0303b76aff9d2b703f74cbed29e0ea822d25a03128edadf7f44da` |
| live_run_evidence.json | `b46207ee1eeb0f9fbf9306049ce0d23a3ca0e9c68c27658ef757a5fa7910e998` |

The retained `service.cpython-313.pyc` hash is `03e19c979d7f04fc88cbf25bf88bfe0956f016565ed52d87710ddda9f3093543`. Reproduction uses source bytes in new directories, not historical bytecode.

## Exact failure and gate sequence

1. `structured_output_schema`: PASS.
2. `scope_security` / Monunius: PASS (the required five filenames were present).
3. `public_verification` / Bato: **FAIL during service import**, before calling `run_acceptance`.
4. `private_verification`, `integration` / Redon, and `reviewer` / Medaurus: **NOT RUN**, due to fail-fast behavior. The old evidence encoded these as `false`; it did not distinguish failure from nonexecution.
5. Zojz's terminal result: APP FAILED.

Exact first reason:

```text
ModuleNotFoundError: No module named 'mini_crm.crm'
```

`service.py` imports `CRMService` from `.crm`, but the owned implementation is `core.py`. There is no retained `crm.py`, and artifact ownership forbids inventing a sixth file. The frozen gate is correct. The old evaluator attempted import before emitting the public-gate start event, explaining why the event list stopped after scope success even though the public gate failed.

## System audit and root cause

The primary architectural defect is an incomplete, contradictory producer/consumer contract and handoff (categories **B/C**). Generated implementation errors (**D**, delivered as syntactically complete provider responses in category **A**) compounded it. There is also a diagnostic/harness defect (**E**): import failure had no gate lifecycle events. No stale state is needed to explain this failure: the exact exception reproduces from retained bytes in a fresh process/directory (**F is not the historical cause**).

At the inspected pre-repair revision `f75e2e2`, `task_prompt()` passed only immediate dependency artifacts. Gentius depends directly on Teuta, so it received `ui.py`, not `core.py`, `database.py`, or `dataflow.py`. That routing is established from source, not from historical prompt logs (none were retained).

The previous fix also told Gentius that create methods return integer IDs and that database operations open separate connections, without imposing those requirements on Agron/Andinus. Both assumptions are false for this run:

- Andinus returns an initialized SQLite connection; Agron retains it as `self.connection` until `close()`. An in-memory database can persist across operations on this connection. File persistence is also supported; foreign keys and status CHECK constraints exist. Importing these modules does not itself open the default database.
- Agron's `create_client`, `create_job`, and `set_status` return dictionaries. Gentius passes the first dictionary directly as a SQL ID. Correcting import/syntax therefore exposes `sqlite3.ProgrammingError: Error binding parameter 1: type 'dict' is not supported`.
- **Independent syntax defect:** `core.py:198` closes a triple-quoted SQL string with two quotes. Python reports `SyntaxError: unterminated string literal (detected at line 204) (core.py, line 204)`. The original `.crm` failure masked this completely.
- Gentius never re-exports `normalize_status`, although the private checks look it up in `mini_crm.service`. With earlier defects removed, the private gate fails with `KeyError`.
- Gentius removes its temporary SQLite file without closing Agron's connection. On Windows, a controlled import/syntax-only correction reproduces `PermissionError: [WinError 32]`, which masks the earlier dictionary-binding exception in `finally`.
- Bindus declares NEW→ACTIVE→DONE transitions, but Agron imports only normalization and does not enforce `can_transition`. Gentius's intended acceptance transitions are legal, so this defect does not explain the first failure.
- Database dashboard keys match frozen acceptance. Teuta's client counter accepts `clients`, `client_count`, or `CLIENTS`, omitting the actual `clients_total`, so it displays zero clients. Agron's `jobs()` returns dictionaries without notes; Teuta consequently displays “No notes” despite persisted notes.
- Gentius's integration probe returns literal client/job counts instead of deriving them from persistence. Its return *shape* matches the gate, but its evidentiary value is insufficient.

Increasing completion tokens again or fixing only the import cannot repair this system. Every response ended with `stop`; there is no evidence of truncation or provider outage in this run.

## Repair at the earliest correct layer

`normal_app_contracts.py` supplies one versioned, machine-readable `mini_crm_interfaces_v2` contract to **all five producers and consumers**. It binds the module names, signatures, integer ID returns, job dictionary keys including notes, dashboard keys, legal transitions, connection lifetime, cleanup ownership, service exports, and acceptance/probe behavior. The new contract deliberately chooses one stable API; it does not pretend the historical dictionary API already conformed.

`normal_app.py` now passes every transitive ancestor's exact source plus hashes and observed AST signatures. Scheduling remains Andinus/Bindus in parallel → Agron → Teuta → Gentius. Fake and real workers receive the same prompt. Source compilation and undeclared sibling-import checks run before COMPLETED. Each task must return exactly one owned `write` patch; duplicate, traversal, foreign-file, and non-write responses fail closed. No retry or overlapping artifact writer is introduced.

`normal_app_gates.py` wraps the **unchanged** shared evaluator in a fresh interpreter and empty workspace. It preserves the frozen predicates, adds the missing import-stage public-gate events, and records PASS/FAIL/NOT_RUN. It rejects a reused workspace and prevents parent-process package caches or simultaneous builds from supplying another run's modules. Generated verification code gets no provider credentials; outbound socket operations are blocked; subprocess failure/timeout remains terminal. This is process/state isolation, not a claim of a complete hostile-code OS sandbox.

`normal_app_fakes.py` contains controlled contract-conforming artifacts for offline checks. Its system test exercises SQLite reopening, actual note persistence, integer IDs, transition rejection, HTML escaping/counts, repeated acceptance/probe calls, and connection closure.

`engine.py` now retains the manifest, source hashes, exact task inputs/outputs, artifact content and original/stored hashes, owner ceiling, call/usage/cost facts, task states, explicit gate states/reasons, and sanitized errors. It scrubs key material, refuses historical-file overwrite, and cannot report READY after evidence persistence fails. `crew.py` makes Andinus follow the actual database task and makes failed Bato/Gentius stages terminal rather than indefinitely active.

The budget repair replaces the old never-updated `_spent` projection and missing in-flight reservations. The provider boundary uses a lock, per-call reservations, actual settled cost, a conservative UTF-8 input envelope, a 45,000-token input cap and an 8,192-token completion cap. The resulting five-call hard envelope is **$0.941520**, within the owner's $1.00 ceiling. Oversized prompts or insufficient budget block before dispatch; unknown-cost failed requests retain their reservation; SDK retries are disabled. The legacy expected estimate remains labeled as a 2K-input reference, not a bound for expanded handoffs.

The pharmacy, inventory multi-station, single-call v6 implementation, frozen evaluator, and Workbench HTML were not edited. No Generic App Factory or product feature work was started.

## Offline proofs and browser evidence

Reproduce with:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/reproduce_normal_app_failure.py --output build/normal-app-forensic-repair/new-reproduction
```

Use a new output directory. This asserts the exact historical reason with the original shared evaluator, then passes the same gates with a controlled correction (one quote repaired in copied `core.py`, plus a service consuming the actual dictionary-returning API, re-exporting normalization and closing connections). The historical database/dataflow/UI remain byte-identical in this diagnostic proof. **This proof alone does not claim the historical UI defects are fixed**; the complete v2 fake factory and system test cover the corrected frontend/backend contract.

`test_normal_app_forensic_repair.py` additionally checks the layered masked failures, full handoff hashes/signatures, ownership, syntax rejection, concurrent/stale import isolation, in-flight budget reservations, oversized-input blocking, partial-failure evidence, API-key redaction, terminal personas, gate-process failure, and evidence-write failure.

Browser run `8054bc031f19` used the real Workbench page on a dedicated loopback port and the real engine with injected fake clients. The browser visibly showed foundation concurrency, then Agron, Teuta and Gentius activity, then APP READY. Persisted evidence records all six gates PASS, all eleven personas VERIFIED, and nine observed crew-state snapshots. Five *fake dispatches* supplied synthetic usage ($0.021200); these are not real provider requests or real spend. No outbound network/provider calls occurred; localhost HTTP was necessary for the real-browser test.

Local proof artifacts:

- `build/normal-app-forensic-repair/reproduction-01/reproduction.json`
- `build/normal-app-forensic-repair/historical_hashes.json`
- `build/normal-app-forensic-repair/browser-01/browser_result.json`
- `build/normal-app-forensic-repair/browser-01/foundation.png`
- `build/normal-app-forensic-repair/browser-01/app-ready.png`

Full validation is run by `scripts/run_normal_app_offline_validation.py`, with credentials removed, live mode off, external DNS/sockets denied, UTF-8 mode enabled, and per-pack logs. REDI-03 requires a clean tracked tree, so validation packs run after committing the repair. Existing locale-sensitive REDI-09 fixture tests require UTF-8 on this Windows host. The Workbench environment-fallback test now uses its provided temporary workspace instead of reusing a fixed historical build directory.

## Owner retry decision

One final owner-triggered live retry is justified **as a bounded experiment after all final validations pass**, not as a guarantee about a new stochastic response. Restart the owner's Workbench process with this repair loaded before retrying; an already running server may still contain the old code. Keep the normal-app backend and $1.00 ceiling.

Expected behavior: five single-attempt calls (parallel foundation, then core, UI, assembly), full upstream handoffs, only the five owned artifacts, successful source/import checks, the same frozen acceptance gates including independent review, and APP READY only if every gate passes and evidence persists. Any new contract/syntax/provider/budget/gate failure must stop without automatic retry, preserve exact diagnostics, and leave unexecuted downstream stages NOT_RUN/PENDING. No live retry was performed during this repair.

## Final validation record

Implementation commit: **`7d80d67f9e40ac0015f855734ca90b6afad97001`**. This final record is a documentation-only follow-up; runtime files remain those tested at that commit.

| Check | Final result |
|---|---|
| Focused forensic / CRM / Workbench / VIRTUTES checks | 60 passed |
| Full unit suite at final implementation commit | 770 tests; REDI-03 full-suite subprocess passed |
| REDI-01 | 77/77 |
| REDI-02 | 24/24 |
| REDI-03 | 10/10, including full units, clean tracked tree and LIVE BLOCKED |
| REDI-04 | 17/17 |
| REDI-05 | 18/18 |
| REDI-06 | 28/28 |
| REDI-07 | 26/26 |
| REDI-08 | 62/62 |
| REDI-09 | 27/27 |
| REDI-10 | 71 tests passed |
| Final committed-source browser run | `606755140a1c`: APP READY, all six gates PASS |
| Historical files rehashed after validation | All original files, including bytecode, unchanged |
| Provider / external network requests during repair | **0 / 0**; browser used loopback HTTP |

The first broad run had 764 passes and three failures: two locale-dependent REDI-09 fixture writes and an existing Workbench test reusing fixed build `envfb1`. UTF-8 mode and that test's own temporary workspace resolved them; the next broad run passed 767 tests before three additional regressions were added. The final REDI-03 run validated all 770.

REDI-10's first pack invocation could not access/delete its pre-existing fixed `build/redi10-pack-pytest` directory (40 fixture-setup errors, 31 passes). The unchanged validation runner was invoked again with **only its `--basetemp` argument redirected** to a fresh directory; all 71 tests passed. Neither the inaccessible old directory nor test predicates were modified. Invocation wrapper and original/final logs are retained in `build/normal-app-forensic-repair/run_redi10_isolated.py` and `packs-final/REDI-10*.log`.

The final browser rehearsal ran after the implementation commit, and every persisted runtime source hash matches that commit. Evidence and screenshot: `build/normal-app-forensic-repair/browser-final/commit_binding.json`, `browser_result.json`, and `app-ready.png`. Both rehearsal servers were stopped; the owner's existing Workbench server was not re-armed or used for a live run.

**Decision: yes, one final owner live retry is now scientifically justified under the conditions above.** The corrected offline path passes the unchanged frozen gates; successful stochastic generation is still an outcome to measure, not a claim made by this repair.
