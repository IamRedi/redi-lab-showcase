# Latest owner normal-app failure: wb-c1564339281f

## Identification and first failure

The latest persisted matching record is `build/workbench-builds/wb-c1564339281f/live_run_evidence.json`, written at Unix time `1790496822.5774677`. Its five calls total **$0.1374480**, matching the owner's observation. All five task states, including Gentius `integration_assembly`, are COMPLETED. Provider failures are empty and every finish reason is `stop`.

| Task | Input | Cached | Output | Cost USD |
|---|---:|---:|---:|---:|
| build_database | 202 | 0 | 1616 | 0.0197960 |
| build_dataflow | 150 | 0 | 686 | 0.0085320 |
| build_core | 1280 | 0 | 3081 | 0.0395320 |
| build_ui | 2059 | 0 | 2814 | 0.0378860 |
| integration_assembly | 2765 | 0 | 2181 | 0.0317020 |
| Total | 6456 | 0 | 10378 | **0.1374480** |

First failing stage: **public_verification**, during import, before acceptance runs. Exact retained reason:

```text
ModuleNotFoundError: No module named 'mini_crm.crm'
```

`structured_output_schema` and `scope_security` passed. Private verification, integration and reviewer did not run; the old record encodes all four unsuccessful/unexecuted gates as `false`. Gentius's returned file was accepted as completed, but its `from .crm import CRMService` points to a nonexistent module; the retained class is in `core.py`.

The five source hashes were verified against completion events:

| File | SHA-256 |
|---|---|
| database.py | `0f047c52b6f2519b50d4adc30f8914fdd9057e9e81435c8426b5b80f145b4609` |
| dataflow.py | `6ade34103644885bc884b6c3f68bbe8df441b5d1d274b5e63868bb5fb6619ac7` |
| core.py | `2689903afba85b11c2eaf9c986b2b1e58dc53dbc1b41bc2bc0b83b915309d2ef` |
| ui.py | `e619681a0f23bb8fbefbf26674cf6ad029a2f45e71b69263f56465446b4f6a15` |
| service.py | `8b0aece225e0840e4dddc9332008ce6c189f226863d57ed2efc53a69ae09d814` |

The complete directory inventory, including evidence JSON and retained bytecode, is hashed in `build/latest-normal-app-forensics/historical_hashes.json`. All originals remain unchanged. Regression fixtures are exact byte copies in `tests/fixtures/mini_crm_wbc1564339281f`; Git attributes prevent newline normalization.

## System audit of this exact output

All five modules parse successfully. The earlier build's SQL syntax defect is **not present** here.

- Database/core imports and SQL schema agree. `initialize_database` returns one initialized connection; core retains it. Foreign keys and SQLite status CHECK constraints are enabled. File-backed persistence works; these modules do not open a database merely by being imported.
- Core create methods return dictionaries, while assembly expects integer IDs. With the import corrected and Windows cleanup masking removed in a diagnostic copy, the next exact failure is `run_acceptance[] raised ValueError: client_id must be a positive integer`.
- Core's `set_status` returns a job dictionary and validates status names, but does not enforce Bindus's `can_transition`. Bindus declares NEW→ACTIVE→DONE. The intended acceptance transitions are legal; lack of enforcement is a secondary mismatch, not the first failure.
- Core returns all five expected dashboard keys. Its job dictionaries include client details and ordered note dictionaries. Teuta consumes those exact keys, including `clients_total`, `jobs_total`, and note `content`. Unlike the earlier build, this frontend/backend handoff is compatible.
- Service imports `.crm` instead of `.core`, omits the private-check `normalize_status` export, treats returned records as IDs, and never closes the retained connection before `TemporaryDirectory` cleanup. On Windows, failed cleanup can mask the underlying ID error. `run_ui` similarly does not close its connection.
- Acceptance returns `dashboard()` with the correct intended counts. Integration derives its counts from the actual dashboard; it does **not** hard-code success counts in this run. Both are blocked by the earlier import/ID/lifecycle failures.
- Reviewer receives the same frozen product checks and retained files; it would reimport service and rerun acceptance/probe. It never executed in the historical run.

## Why the prior repair was not exercised

The decisive cause of this repeat incident is **a stale owner Workbench process**, not evidence that the newly repaired contract path produced these files.

Read-only requests to the existing local server returned PID **38772**, server start **2026-09-27 07:21:33.669641 UTC**, and the exact failed build snapshot. The prior implementation repair was committed at **07:55:09 UTC**. This build ran at **08:12:18.888315–08:13:42.576829 UTC**, after that commit but inside the older process.

This conclusion does not rely only on timestamps:

1. The persisted record has the old shape: no evidence schema version, manifest, task prompts/outputs, source hashes or explicit gate states.
2. It lacks the `planning_started` and `scope_resolved` events that the repaired runner unconditionally emits.
3. It marks `service.py` COMPLETED despite `.crm`. Injecting these same bytes into the repaired runner fails `integration_assembly` with `ValueError: undeclared local import: .crm` before gates.
4. The old missing import-stage gate events appear again, whereas the repaired adapter emits public-gate start/fail around import.

The old record did not retain original prompts or an executing revision; its exact old Git SHA cannot be recovered as a persisted fact. Its behavior is incompatible with the repaired implementation. Changing files on disk did not replace modules already loaded by the server. The previous final report required a restart, but there was no enforced process/source freshness preflight.

## Smallest general repair

No additional prompt workaround or provider-output adaptation was added to production.

- `runtime_identity.py` captures the normal-app source cohort once when imported by the engine and compares disk hashes thereafter. Its identity includes process ID, capture time, fingerprint, original source hashes, current disk hashes and changed/unreadable files. It is an import-time source snapshot, not a claim of a cryptographic in-memory bytecode attestation.
- Engine preflight blocks stale normal-app runtimes **before client construction** and persists the BLOCKED reason with zero calls. The provider boundary checks again before every dispatch, so an edit between tasks cannot silently mix revisions. The normal-app runner also checks before and after gates.
- `/api/server` reports this identity through the existing read-only diagnostic endpoint. Future evidence stores the original process snapshot as `source_hashes`, rather than reading current disk files at run completion and implying those bytes were executed.
- The guard cannot retrofit a legacy process that never imported it. The existing owner server was neither stopped nor re-armed. Its missing runtime identity is itself a reason not to retry through it.

Artifact ownership, budgets, frozen gates and VIRTUTES outcomes are preserved. Pharmacy, inventory multi-station, single-call v6, the shared evaluator, and Workbench HTML were not modified. No live build was retried.

## Isolated Total time fix

Historical events span **83.688513517 seconds**. The Result snapshot reported zero because `WorkbenchBuild.snapshot()` first calculated the real elapsed time, then overwrote it with `live.get('elapsed_seconds', 0)`. The normal-app result does not supply that field. The page directly renders the snapshot value.

Normal-app snapshots now retain the build's start/finish-clock duration; terminal snapshots remain stable. Evidence also records elapsed seconds. Other backend timing behavior and execution semantics are unchanged. Regression tests cover RUNNING, VERIFIED, FAILED and BLOCKED without sleeping for 83 seconds.

## Offline proof and retry decision

`tests/unit/test_latest_normal_app_failure.py` reproduces the exact original reason using both the unchanged shared evaluator and the isolated adapter. A controlled service consuming the actual retained record-returning API, importing the correct modules and closing connections passes **the same frozen gates**, while the four upstream files remain byte-identical. The fresh generic factory also passes with injected fake providers.

Additional regressions prove stale-source detection, missing-source failure, zero client construction/dispatch on stale preflight, blocking edits between calls or during gates, durable identity, source-preserving retention, and corrected elapsed time. Evidence: `build/latest-normal-app-forensics/reproduction.json`, `owner_server_readonly.json`, and the focused/validation logs.

**Another live retry is not justified through the current owner server.** After an explicit restart into the repaired checkout, the owner must first confirm `/api/server` exposes `normal_app_runtime.fresh = true` with the expected current source fingerprint. Then one bounded owner retry is justified by the offline proofs. No additional prompt change can repair an already-running legacy process. No real provider request was made during this pass; the only HTTP requests were read-only localhost diagnostics and offline server tests.

## Final validation record

Implementation commit: `6a63e9820ff4f7018b3dab74aca7a6cb096d1437`.

- Focused forensic, contract and normal-app regressions: **55 passed**.
- Full `tests/unit/` suite (785 collected tests): **PASS**, executed by REDI-03 v01. REDI-03 also confirmed the tracked working tree was clean.
- REDI-01 through REDI-10: **all PASS**, respectively 77, 24, 10, 17, 18, 28, 26, 62, 27 checks and 71 REDI-10 tests.
- Validation subprocesses had credentials removed and external socket/DNS access blocked. REDI-03's live branch explicitly reported LIVE BLOCKED because its API key was unset.
- REDI-10 used its unchanged runner, test selection and assertions, with only `--basetemp` redirected because its legacy fixed temporary directory is inaccessible. No historical directory was removed or altered.
- Post-validation hashes match every original retained file, including the historical evidence JSON and bytecode.

Machine-readable pack results: `build/latest-normal-app-forensics/validation/results.json`; adjacent logs retain each pack's result. The final read-only owner check still found PID **38772** without runtime identity. Expected fingerprint for a fresh process from this source cohort: `4418969484b8ec8163bf51c9068e129b9626c9d485cea0755e8e83e2badafc99`.
