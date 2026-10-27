# Normal-app factory closure

## Forensic findings

Latest owner failure: **wb-67e4bed07ea3**, five real calls, **$0.1360940**. First failure is public verification while importing service: `ModuleNotFoundError: No module named 'mini_crm.crm'`. Schema and scope passed; private/integration/reviewer did not execute. All five tasks had already been marked COMPLETED.

The original evidence JSON and every retained artifact were hashed before work. Exact-byte fixtures are retained in `tests/fixtures/mini_crm_wb67e4bed07ea3`. The unchanged isolated frozen evaluator reproduces the exact gate/reason. `core.py` also has an unterminated triple-quoted SQL string, hidden by service's earlier missing-module import. Correcting that thrown line alone would therefore be insufficient.

| Owner build | Calls | USD | First failure / recurring defect |
|---|---:|---:|---|
| wb-1ac58eb0bb0d | 3 | 0.0561720 | Core JSONDecodeError after empty length-capped completion; output cap exhausted at 2048 |
| wb-3840d7b30163 | 5 | 0.1314740 | Public acceptance: `AttributeError: 'int' object has no attribute 'id'`; return shape and hidden dashboard/acceptance contracts |
| wb-847df3c91090 | 5 | 0.1351840 | Missing `.crm`; underlying core syntax and assembly/interface mismatches |
| wb-c1564339281f | 5 | 0.1374480 | Missing `.crm`; dictionary-returning core consumed as IDs, missing export/lifecycle |
| wb-67e4bed07ea3 | 5 | 0.1360940 | Missing `.crm`; core syntax, record/ID mismatch, missing export, UI schema drift and browser-only state |

All five records lack runtime identity and original prompts. Their exact executing source SHA and per-call requested token caps cannot be recovered from those records. The first run's retained length/output facts establish the 2048 truncation; later repairs configured 8192, but that is source history, not a retained request fact. The complete inventory in `build/normal-app-takeover/failure_timeline.json` retains per-task states, call/token facts, reason fields and source hashes. Injected test records are kept separate from this owner timeline; future records explicitly label real-provider versus injected execution.

The answering owner server was still PID 38772, started at Unix `1790493693.6696405` (07:21:33 UTC), before both prior repairs. `/api/server` lacked the identity field; all three missing-module runs retained the old evidence/event shape and accepted imports the newer validator rejects. This is evidence of old loaded code, not evidence that the repaired prompts were exercised. A source-only repair plus an owner restart instruction did not close the deployment boundary.

## Contract matrix for the latest output

| Producer -> consumer | Required/declarative interface | Actual latest output | Delivery/effect |
|---|---|---|---|
| Database -> core | `connect(path)` initializes SQLite; caller owns connection; canonical counts | `connect(database)` opens only; separate `initialize_database`; core knows this historical API | Compatible historical pair, incompatible with current declared contract; no original prompt retained |
| Dataflow -> core | normalize status AND enforce legal transitions | Core imports normalize only; ignores `can_transition`; malformed SQL also prevents import | No execution check before COMPLETED |
| Core -> assembly | Shared create-return convention and real module name | Core returns client/job dictionaries; assembly treats these as IDs and imports nonexistent `.crm` | Complete shared contract was not evidenced in this legacy run |
| Core -> UI | canonical five dashboard keys; jobs include notes | UI reads `clients`/`client_count`, not `clients_total`; core jobs omit notes | Handoff did not enforce consumer compatibility |
| UI -> persistence | user actions invoke real service/SQLite operations | JavaScript mutates local arrays only; edits disappear and never reach SQLite | Frozen gates do not exercise browser mutations |
| Assembly -> public gate | callable `run_acceptance`, real counts, isolated file lifecycle | Wrong module; ID mismatch; SQLite connections not closed before temporary cleanup | Public import fails before acceptance |
| Assembly -> private gate | re-export `normalize_status` | Missing export | Hidden downstream failure; gate NOT_RUN |
| Assembly -> integration/reviewer | callable probe, real counts, independent repeated runs | Probe derives counts correctly in principle, but cannot import/execute; lifecycle unsafe | Both gates NOT_RUN |

## Repair layer

The existing product-family contract is now version 3, with machine-readable keyword call shapes and HTTP form routes supplied to every worker alongside the complete transitive source handoff. The new executable integrity layer runs in a separate credential-free interpreter with fresh package/SQLite state. It runs **before each station can complete**, then again on the complete product before frozen gates. No build-ID/provider-output adaptations are used by the factory.

Checks cover every Python file's syntax, actual imports, public symbols/callability, keyword-compatible signatures, return types, canonical schema/keys, status constants/transitions, IDs, notes, foreign keys, initialization, idempotent close, leaked connections, close/reopen persistence, import-time side effects, escaped UI rendering, usable POST forms, acceptance/probe callability and repeat isolation. Reports are retained separately from gate states. The six frozen predicates remain unchanged; no integrity success can replace a gate success.

The trusted local `product_host` is transport only. It invokes generated `build_crm`, core operations and `run_ui` through the declared route contract; it does not synthesize data, rewrite generated outputs or supply a replacement CRM. The UI must submit real forms. A localhost end-to-end test creates a client/job, changes status, adds notes, checks actual counts and restarts the host against the same SQLite file.

Runtime freshness includes the integrity and host sources. Station completion events now mean the executable interface checks passed, preserving evidence-exact VIRTUTES semantics. Owner budgets, one call per task, zero automatic retries, artifact ownership, pharmacy and single-call v6 paths remain intact. Exact token caps and execution provenance are retained for future calls. Historical evidence is never overwritten.

## Offline evidence

- `build/normal-app-takeover/exact_replay.json`: unchanged latest bytes reproduce the historical failure.
- `build/normal-app-takeover/offline_ready.json`: repaired Workbench path reaches APP READY with five injected calls and all frozen gates PASS.
- `build/normal-app-takeover/focused2.log`: 75 focused tests passed, including sixteen deliberately defective contract variants and the localhost/persistence workflow.
- `build/normal-app-takeover/historical_hashes.json`: original directory inventory used for final immutability checks.

## Validation and initial approval blocker (subsequently resolved)

Implementation commit: `01876f78fd5a8597d3e2838cd30a0fceedd7866a`.

Full unit suite: **PASS** through REDI-03 v01; separate collection confirms **805 tests**. All REDI-01 through REDI-10 packs passed (77, 24, 10, 17, 18, 28, 26, 62, 27 checks and 71 REDI-10 tests respectively). The validation wrapper removed credentials and blocked external socket/DNS access. REDI-03 explicitly skipped its live branch because the key was absent. REDI-10 used the unchanged runner with only its inaccessible legacy `--basetemp` redirected. Tracked working tree cleanliness passed. All **69** original normal-app directories remain byte-for-byte unchanged after validation.

The runtime inventory confirms exactly one answering Workbench server: **PID 38772**, the old process without runtime identity. Its API reports a live key and enabled flag. The replacement/tool environment and Windows user/machine environment have no configured key. A fresh process from the repaired source reports fingerprint `17bd6172f7c983ac36b2596ae051476f1d9d49b265567489858941018bae52fd`.

**External blocker: permission to transfer the existing live credential into the replacement process.** Automatic approval review rejected the proposed narrow in-memory environment transfer, classifying process-memory credential extraction as not explicitly authorized. No credential value was read, displayed or saved; no rejected action was bypassed. Explicit approval was requested and remained pending when this record was written. Code changes cannot supply a missing authorized credential or override that review decision. The old process was preserved to avoid losing its environment.

Consequently the single paid acceptance was **NOT RUN**: no final real build ID, zero new provider calls/tokens/spend, no real gate outcomes, and no claim of real APP READY. Offline APP READY and all six frozen gate PASS outcomes are proven; the generated offline product's actual HTTP/backend/SQLite path and restart persistence are tested. There is no provider/capability failure to report because no new provider request was submitted.

The prepared controller at `build/normal-app-takeover/run_one_live.py` verifies all validation results, matching fresh runtime identity and process-local live environment, reserves an exclusive durable attempt marker and sends at most one POST through the actual Workbench API with the exact owner request and $1 ceiling. It has not been executed. Remaining work after transfer approval is the authorized server replacement, that single controlled build and verification of its actual generated product. The owner is not being asked to diagnose or retry a build.

Evidence: `validation/results.json`, `server_process_inventory.json`, `server_final_readonly.json`, `expected_runtime.json`, `owner_failure_comparison.json`, and `final_status.json`, all under `build/normal-app-takeover/`.

## Authorized single live acceptance

The owner subsequently explicitly authorized transferring ONLY `REDI_OPENAI_API_KEY` and `REDI_LIVE_PROVIDER` from PID 38772 into the replacement process's memory. That transfer completed without displaying, logging, writing or persisting either credential value. The transfer helper's first invocation stopped before server mutation because its repository import path was missing; fixing that helper-only path did not change the tested factory or submit a build.

PID **38772 was stopped**. The repaired checkout at implementation `01876f78fd5a8597d3e2838cd30a0fceedd7866a` / report `0cda86cc716934bbe3cf2620e87d8019fe6c3bec` started one Workbench server, **PID 40724**, with exactly one listener at **127.0.0.1:8765**. Windows' virtual-environment launcher PID 34420 is its parent, not another listening server. Both `key_present` and `live_flag_is_1` are true; runtime fingerprint matches `17bd6172f7c983ac36b2596ae051476f1d9d49b265567489858941018bae52fd`, and remained fresh after execution.

Exactly one real build was submitted through `/api/builds`: **wb-ceab8c955c8b**. The exclusive attempt marker prevents a second POST. Result: **APP FAILED**, elapsed **23.195 seconds**, **4 real provider calls**, **9,314 input tokens**, **2,520 output tokens**, no cached input, **$0.0488680** spend. Every completed provider response had finish reason `stop`; there was no provider transport failure and no automatic retry.

| Stage | Outcome |
|---|---|
| Database / Andinus | COMPLETED; executable integrity PASS |
| Dataflow / Bindus | COMPLETED; executable integrity PASS |
| Core / Agron | COMPLETED; executable integrity PASS, including real persistence and transitions |
| UI / Teuta | FAILED during executable integrity, before completion |
| Assembly / Gentius | PENDING; not called |
| Schema, scope, public, private, integration, reviewer | All NOT_RUN; blocked before final gates |

**Exact remaining defect:** `IntegrityError: KeyError: 'font-family'`. The generated `ui.py` builds its HTML with a triple-quoted string followed by `.format(...)`. Literal CSS such as `body{font-family:sans-serif;...}` has single braces, so Python interprets `font-family` as a replacement field. Rendering fails before the page can be displayed. Replaying all four unchanged retained modules through the same integrity checker reproduces the exact exception offline.

This is a provider-generated implementation defect, **not an external service outage or an unfixable code defect**. The integrity layer correctly prevented false station completion, a wasted assembly call, and a false APP READY verdict. Under the owner's explicit instruction to stop after this single real build, no second provider/build attempt and no one-off generated-output rewrite was performed. A real runnable product was therefore not achieved; there is no successful product launch command to report.

All original 69 evidence directories and the new live evidence remain unchanged. No production source changed during this acceptance. The previously completed 805-test full-suite validation, 75 focused tests and REDI-01 through REDI-10 results apply to the exact executed factory revision. Evidence is retained in `build/workbench-builds/wb-ceab8c955c8b/live_run_evidence.json` and `build/normal-app-takeover/{live_attempt.json,live_observation.jsonl,live_final.json,authorized_live_result.json,final_live_hashes.json,server_after.json,listener_verified.json}`. This section supersedes the earlier pending-approval status.
