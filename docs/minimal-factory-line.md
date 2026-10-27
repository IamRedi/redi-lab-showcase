# REDI-03 Minimal Factory Line

A single-product, single-provider, single-Builder, single-Reviewer deterministic
event-driven production line.  All state transitions are persisted to the
append-only evidence ledger before observable state changes; every process
exit (normal, exception, synthetic crash) can be resumed from the ledger
without re-executing the Builder worker.

## 1. State machine

The canonical run FSM is encoded in `MinimalFactoryController.run` and the
static transition table is expressed by the event sequence appended to the
evidence ledger.  Start state = `PENDING`.  Transitions are evidence-driven;
no in-memory variable encodes the "current state" outside the ledger event
ordering.

Entry points:
- [MinimalFactoryController.run](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L246-L253) — fresh run.
- [MinimalFactoryController.resume](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L703-L718) — post-crash recovery.

Observed event → terminal mapping:
- `PENDING` → `BUILDING` on `WORKER_STARTED` (controller L305 + worker invokes).
- `BUILDING` → `WORKER_COMPLETED` persisted → `GATING` begins.
- `GATING` → on `GATE_PASSED` → `REVIEWING`; on gate failure + budget
  remaining → `RETRY_REQUESTED` [controller L450-L458](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L450-L458);
  budget exhausted → `WORKER_ESCALATION_REQUIRED`
  [controller L504-L505](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L504-L505)
  → terminal `FAILED` / `BLOCKED`.
- `REVIEWING` → `NODE_COMPLETED` → reviewer `PASS` → `PRODUCT_VERIFIED`
  → final `RunStatus.COMPLETED + verified=True`; reviewer `FAIL` →
  `FAILED`; reviewer `ESCALATE` → `BLOCKED` with escalation flag set.
- Any unexpected exception → failure record with
  `FailureCategory.ENVIRONMENT_FAILURE` → terminal `FAILED`.

Crash injection boundary: `SimulatedCrash` raised **after** `conn.commit()`
following `WORKER_COMPLETED` evidence append, so resume() must observe the
persisted `WORKER_COMPLETED` and continue at GATING without re-running the
Builder
[controller L419-L423](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L419-L423).

## 2. Builder boundary

The Builder worker is a [WorkerRunner](file:///c:/All%20my%20app/Redi-Lab/redi_lab/runtime/contracts.py#L67-L90)
protocol instance (`run_node(context) -> NodeRunResult`).  Structured
output: 0…N [PatchEdit](file:///c:/All%20my%20app/Redi-Lab/redi_lab/runtime/contracts.py#L31-L43)
records with action ∈ {write, delete}, `rel_path`, `content`,
`content_sha256` hash, and metadata.

Context construction is keyword-only at
[build_builder_context](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/builder_context.py#L104-L183).
Context fields:
- `project_inputs` — fixture files in `allowed_scope_rel_prefixes`.
- `source_references` — public-only test globs; hidden test directory is
  filtered out by `_is_hidden` predicate
  [builder_context.py L79-L84](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/builder_context.py#L79-L84)
  before any serialization.
- `explicit_exclusions` — paths rejected by hidden/secret/default-exclusion
  predicates with reason codes including `SECRET_BLOCKLIST_EXCLUSION_CODE`
  [builder_context.py L31-L39](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/builder_context.py#L31-L39).

The Builder **cannot self-approve**: the reviewer branch is reached only if
all four deterministic gates PASS; gate failure → RETRY_REQUESTED or
`WORKER_ESCALATION_REQUIRED` without invoking the reviewer.  No code path
lets the Builder emit `PRODUCT_VERIFIED`.

## 3. Reviewer independence

Reviewer worker is a *separate* `WorkerRunner` instance, bound to a
different `StationContract` (role `"reviewer"` vs. `"builder"`) and a
distinct `worker_id`/`station_id` pair passed at
[RunDeps](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L74-L88)
construction.

Context materialized at
[build_reviewer_context](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/builder_context.py#L184-L266)
contains only: product spec, task contract, station, before/after text for
files inside scope (diff-style), anonymised gate summaries (no hidden-test
identifiers, no raw exit-code outputs redacted through
[reasons_clean filter](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/builder_context.py#L228-L228)).

All serialized context fields pass `_SECRET_NAME_RE.sub("[REDACTED]", …)`
before being handed to the worker
[builder_context.py L259-L259](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/builder_context.py#L259-L259).

Verdict type: [ReviewerVerdict](file:///c:/All%20my%20app/Redi-Lab/redi_lab/runtime/contracts.py#L24-L29)
`PASS | FAIL | ESCALATE` plus optional `reasons` and `ref_ids`.
Controller propagates: `PASS → COMPLETED verified=True`;
`FAIL → FAILED verified=False`; `ESCALATE → BLOCKED verified=False`
with `FinalRunOutcome.escalation = True`.

## 4. Deterministic gates

Four gates, fixed order, short-circuit FAIL (mandatory=True — none are
skippable).  Implemented in
[DeterministicGateEngine.run_all](file:///c:/All%20my%20app/Redi-Lab/redi_lab/gates/__init__.py#L113-L163):

1. `allowed_scope` — compare actual file hashes against `GateSpec.baseline_hashes`
   baseline snapshot; `_changed_relpaths` ignores `__pycache__/`, `.pyc`,
   `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.coverage`
   [gates L82-L108](file:///c:/All%20my%20app/Redi-Lab/redi_lab/gates/__init__.py#L82-L108).
2. `python_syntax_compile` — `py_compile.compile(path, doraise=True)` on
   every changed .py file
   [gates L195-L223](file:///c:/All%20my%20app/Redi-Lab/redi_lab/gates/__init__.py#L195-L223).
3. `public_test_execution` — `pytest … tests/` inside the workspace copy;
   `cwd=<ws>`; pytest flags include `-p no:cacheprovider` on both
   [GateSpec L43-L44](file:///c:/All%20my%20app/Redi-Lab/redi_lab/gates/__init__.py#L43-L44)
   to prevent `.pytest_cache/` from leaking into scope-change detection.
4. `hidden_test_execution` — pytest executed with `cwd=<hidden_test_root>`
   (directory **outside** the fixture tree); `PYTHONPATH` rebuilt
   explicitly as `[ws_root, hidden_cwd]` with no inheritance or
   duplication, and no stale copies of the fixture
   [gates L239-L244](file:///c:/All%20my%20app/Redi-Lab/redi_lab/gates/__init__.py#L239-L244).

Gate subprocess defaults: `per_gate_timeout_s = 30`,
`output_cap_bytes = 64 * 1024`; stdout/stderr captured, truncated, and
appended to gate-result metadata only — never surfaced to the Builder.

## 5. Isolation model

Workers mutate a temporary copy, never the fixture originals.  Abstraction:
[IsolatedWorkspace](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/isolation.py#L64-L158)
— creates `parent = tempfile.mkdtemp(prefix="redi_ws_")`, copies
`fixture_root` → `<parent>/ws`; `destroy()` calls `shutil.rmtree(parent)`
via weakref finalizer, so even leaked workspace handles clean up.

Scope guard: [apply_patches](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/isolation.py#L159-L210)
returns `(ok: bool, violations: list[ScopeViolation])` — performs **all**
path validation first (no `..` parts, rel_path must start with an allowed
prefix), then writes atomically on a fully-validated patch list; if any
patch violates scope, **zero** writes happen and the workspace is left in
its pre-call baseline.  Low-level path check at
[assert_in_workspace](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/isolation.py#L49-L62)
with realpath + startswith guard.

Retry isolation: `IsolatedWorkspace.reset()` tears down `<ws>` and re-copies
`fixture_root`, so attempt N+1 starts from a clean baseline independent of
mutations performed during attempt N.

## 6. Retry policy

Bounded retries configured via
[RunDeps.max_retries](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L86-L86):
`max_retries = N` → at most `N+1` total attempts.

Budget accounting in
[MinimalFactoryController.run](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L305-L508):
- attempt_index ∈ 0..max_retries.
- After a failed mandatory gate → `RETRY_REQUESTED` event persisted with
  `budget_remaining = (max_retries - attempt_index)` metadata field,
  `attempt_index` incremented, `ws.reset()` re-baselined for next attempt.
- If `attempt_index >= max_retries` → `WORKER_ESCALATION_REQUIRED`
  appended (`escalation_level = "WORKER_ESCALATION"`) and the run exits
  terminal `FAILED`.

Escalation semantics: reviewer `ESCALATE` verdict, gate budget exhaustion,
or unrecoverable environment failure → `FinalRunOutcome.escalation = True`.
The caller (Experiment harness, not REDI-03) decides human intervention /
product-class fallback.

## 7. Crash / resume

Resume contract: if process A commits `WORKER_COMPLETED` + `conn.commit()`
then dies (e.g. `SimulatedCrash`), process B given the identical
`factory_run_id`, product, task, deps, and `db_path` must deterministically
reach the same outcome without calling `deps.builder_worker.run_node(...)`
even once.

Implementation at
[MinimalFactoryController.resume](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L703-L995):

1. Probe the `factory_runs` SQL shadow column `closed_at` via **direct
   SELECT** (never `close_run` as a probe — that would write to the row and
   break the single-write invariant)
   [controller L727-L750](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/controller.py#L727-L750).
   If `closed_at IS NOT NULL` → run already finished; return reconstructed
   `FinalRunOutcome` without touching workers or gates.
2. Otherwise replay the latest persisted `RunEvidence` → extract
   `patches_applied` from evidence payload.
3. Rehydrate workspace: fresh `IsolatedWorkspace(fixture_root)`, then
   `apply_patches(ws, saved_patches, allowed_scope=…)`.
4. Optional `builder_run_guard()` callback fires exactly once right before
   the gate engine — used in tests to `assert builder_worker.call_counter
   == 0`.
5. Run the 4-gate deterministic pipeline, then the reviewer, then
   `close_run(...)` with the final `WHERE id=? AND closed_at IS NULL`
   guard so two racing resume() invocations cannot double-close.

## 8. Evidence integration

Every transition emits to the REDI-02 append-only ledger.  Controller uses
the EvidenceLedger helpers:
- `append_event` → `LifecycleEvent` rows (PENDING start, WORKER_STARTED,
  WORKER_COMPLETED, RETRY_REQUESTED, WORKER_ESCALATION_REQUIRED,
  GATE_PASSED / GATE_FAILED, NODE_STARTED/COMPLETED, PRODUCT_VERIFIED,
  ARCHITECTURAL_DECISION_REQUIRED).
- `append_run_evidence` → Builder + Reviewer context/output pairs with
  deterministic `content_sha256` on each PatchEdit.
- `append_gate_result` → gate identity, status, failure_reasons, metadata
  (stdout/stderr truncated, exit code).
- `append_failure_record` → `FailureCategory` enum + origin + recoverable
  flag.

Factory run row: created with `final_status = RunStatus.PENDING`,
`verified = False`, `closed_at = None`.  Exactly one write mutates those
shadow columns: `FactoryRunRepository.close_run(status, verified, …,
WHERE id = ? AND closed_at IS NULL)` — payload_json (the original
`FactoryRun` typed contract) is NEVER updated; `read()` always returns the
preregistration-time PENDING view.  Callers that need run state query SQL
directly (as resume() does for the closed_at probe).

## 9. Hidden-test isolation

Canonical slug fixture layout:
- `experiments/fixtures/slugify_pkg/` → public product under test (copied
  into the isolated workspace, visible to Builder).
- `experiments/fixtures/slugify_pkg_hidden_tests/` → sibling directory,
  **never** copied into the workspace, never referenced in Builder or
  Reviewer context.

Three-location guarantee:
1. `make_canonical_slug_fixture_refs()` returns a dict with three distinct
   keys (`fixture_root`, `hidden_test_root`, `allowed_scope_prefixes`,
   `gate_spec`) — `hidden_test_root` is never equal to fixture_root or any
   subdirectory thereof.
2. `build_builder_context` drops `_is_hidden(rel)` files
   [builder_context.py L133-L137](file:///c:/All%20my%20app/Redi-Lab/redi_lab/factory/builder_context.py#L133-L137)
   and annotates exclusions with reason codes.
3. Gate 4 (`hidden_test_execution`) runs pytest with `cwd =
   deps.hidden_test_root`, and `PYTHONPATH = [ws_root, hidden_cwd]`
   [gates L239-L244](file:///c:/All%20my%20app/Redi-Lab/redi_lab/gates/__init__.py#L239-L244)
   so test imports resolve `slugify` from the patched workspace copy, but
   the hidden test source files are never reachable from within the
   workspace sandbox.

## 10. Live-provider configuration

The live adapter (`LiveProviderAdapter`) follows exactly the same
`WorkerRunner` protocol as fake workers.  Configuration is purely
env-driven.  Nothing is hard-coded.

Type and constructor:
[LiveProviderConfig](file:///c:/All%20my%20app/Redi-Lab/redi_lab/runtime/live_provider_adapter.py#L43-L52):
- `provider: str = "openai"` — single allowed live provider for REDI-03;
  multi-provider recipes deferred.
- `model: str = "gpt-4o-mini"` — cheap, well-scoped model.
- `env_key_name: str = "REDI_OPENAI_API_KEY"` — never persisted or logged.
- `max_tokens: int = 2048`, `timeout_s: float = 60.0`.

Key safety:
- Key retrieval at `os.environ.get(self.config.env_key_name, "")` — if
  unset or empty → raises `LiveProviderBlockedError` with a user-facing
  message and exit 0 semantics from validation runners
  [live_provider_adapter.py L75-L78](file:///c:/All%20my%20app/Redi-Lab/redi_lab/runtime/live_provider_adapter.py#L75-L78).
- Ledger scrub: any string field returned to evidence (model ref, raw
  content, error text) passes `_scrub_for_ledger`, which redacts
  `HOME|USERNAME|KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|APIKEY|\.env`
  tokens with `[REDACTED]`
  [live_provider_adapter.py L53-L58](file:///c:/All%20my%20app/Redi-Lab/redi_lab/runtime/live_provider_adapter.py#L53-L58).

Blocked signals:
- SDK not importable,
- env key missing / empty,
- provider call raised,
- non-200 wrapped in `LiveProviderBlockedError` so tests/validation runners
  can catch it and log `LIVE BLOCKED: <reason>` without propagating a trace
  or any secret-containing text.

## 11. Deliberately deferred functionality

REDI-04+ territory explicitly NOT implemented in the minimal factory line:

- **Multi-provider / provider routing.** Only one live provider is wired
  (`openai`).  No `provider_pool`, no routing heuristics, no fallbacks.
- **Worker pools, parallelism, concurrency.** A single sequential run; no
  thread/process pool; no async; no distributed execution.  Resume() is
  single-threaded same-process.
- **DAG routing / multi-node recipes.** `dag_id =
  "canonical_slug_fixture_line_v1"` hard-coded; one Builder, one Reviewer,
  four gates; no DAG interpreter, no conditional edges, no join/split
  nodes, no `Recipe` or `RecipeRung` traversal.
- **REDI-04 UI, Astra SaaS, HTTP endpoints, docker, k8s, temporal.** Zero.
- **Hidden-test discovery within fixture.** Tests MUST be in a sibling
  external directory; REDI-03 never walks fixture sub-trees for nested
  hidden markers.
- **Long-running evidence compaction / archival.** Ledger grows
  append-only; no pruning, no sharding, no export/import across DBs.
- **Model-assisted gate override.** Deterministic gates fail the run on
  FAIL; no model judgment is permitted to "pardon" a failed gate.
- **Interactive / streaming Builder output.** Builder returns a single
  synchronous `NodeRunResult`; no partial patches, no streaming diffs.
