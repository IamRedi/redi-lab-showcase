# UI rendering integrity repair

## Cause and exact reproduction

The retained `ui.py` from **wb-ceab8c955c8b** applies `.format(...)` to an HTML document whose CSS contains unescaped literal braces. Rendering the original source raises exactly **`KeyError: 'font-family'`**. The factory retained `IntegrityError: KeyError: 'font-family'`, failed Teuta and blocked assembly and all six frozen gates.

The original rendering smoke was already executing before station completion; its fail-closed behavior worked. The missing abstraction was an explicit, enforceable **generation-time rendering strategy**. A prompt that specifies only keys and forms leaves markup/template syntax to the worker. This repair does not rewrite any historical or newly generated provider output.

The exact UI bytes and their recorded SHA-256 are preserved in `tests/fixtures/mini_crm_wbceab8c955c8b/`. The original evidence directory is hashed in `build/ui-integrity-repair/historical_hashes.json`. `original_reproduction.json` records the unchanged-source exception.

## Generic repair

The shared contract is now `mini_crm_interfaces_v4`; existing Python signatures, ownership and backend routes are preserved. Its UI-specific `literal_markup_composition_v1` policy is included in Teuta's prompt **before generation**:

- Compose HTML using literal strings, concatenation and `str.join`. Keep CSS braces literal and insert `html.escape(str(value))` once.
- Do not use `.format`, `.format_map`, f-strings, percent interpolation or template-engine substitution in generated `ui.py`. The small AST preflight rejects these constructs before import; it is contract enforcement, not a transformation of provider bytes.
- Render the five dashboard counts in individually identifiable `data-count` spans, allowing deterministic comparison to backend values and detection of unfilled count placeholders.

`product_integrity.py` executes the renderer in the existing isolated, credential-free process on empty, populated and special-character data. Data includes literal CSS-like braces, dollar placeholders, percent signs, quotes and HTML characters. Checks require actual HTML, escaped client/title/note text, unchanged inputs, exact dynamic counts, all required POST forms/fields and declared backend actions. Unknown routes, external resource targets and form transport overrides fail. Actual backend-derived rendering and persistence checks remain in place as well.

Teuta's completion event is emitted only after these checks pass. A regression holds the renderer check pending and confirms WORKING; after success it confirms VERIFIED. A rendering KeyError produces FAILED, leaves assembly PENDING and all gates NOT_RUN. No VIRTUTES state rules or frozen gate predicates were changed.

Pharmacy, single-call v6, budget reservation, ownership, the shared evaluator and server transport remain untouched. No real provider call, live retry or external network request was made. Browser traffic was localhost only; the test server had no provider credentials and blocked external sockets/DNS.

## Offline and real-browser proof

- Unchanged retained UI reproduces `KeyError: 'font-family'`.
- The generic safe UI passes the expanded checker with the **unchanged retained database/dataflow/core**, recorded in `generic_ui_proof.json`.
- **92 focused tests passed**, including negative formatting/placeholder/route cases and Teuta completion-order tests.
- A real Chrome session submitted the owner request to an isolated Workbench with injected deterministic providers. Fake build **wb-145d89cd7ff1** reached **APP READY through all six unchanged frozen gates**.
- While UI integrity was deliberately held, Chrome showed Teuta **WORKING**, with Gentius and final gate personas **WAITING**. Only after release and successful rendering did the build complete.
- Chrome forms created a client/job, submitted NEW -> ACTIVE -> DONE, added notes containing `${missing}` and literal braces, and reloaded. Dashboard counts and displayed data matched the persisted SQLite rows. The fake usage/cost values displayed by Workbench are injected test facts, not real provider usage or spend.

Browser text and screenshots are under `build/ui-integrity-repair/browser/`, including `teuta-working.png`, `workbench-ready.png`, `product-verified.png` and `persisted_state.json`. The temporary browser tabs and credential-free test server were closed after verification.

## Final validation

Implementation commit: **`cc37912db7491de9b945d124e09c622d45f0db25`**.

- Focused tests: **92 passed**.
- Full unit suite: **PASS** through REDI-03 v01; **822 tests collected**. REDI-03 confirmed the tracked working tree was clean.
- REDI-01 through REDI-10: **all PASS**, respectively 77, 24, 10, 17, 18, 28, 26, 62, 27 checks and 71 REDI-10 tests.
- The offline wrapper removed API keys and blocked external socket/DNS calls. REDI-03's live branch explicitly remained BLOCKED. REDI-10 used its unchanged runner/test selection with only the inaccessible legacy `--basetemp` redirected.
- All **70** historical normal-app evidence directories, including the latest failed UI build, remain byte-for-byte unchanged after testing.
- Browser fake-provider verification: **PASS**, with all six frozen gates PASS and real form-to-SQLite operations verified. Actual provider calls and external network requests: **zero**; browser HTTP traffic was localhost only.

Pack logs and machine-readable results are in `build/ui-integrity-repair/validation/`; `build/ui-integrity-repair/final_validation.json` records the final proof summary.

One bounded future live acceptance is justified by this offline evidence, subject to a **fresh Workbench process matching the committed sources**. The existing live server is deliberately not restarted or used during this zero-provider task; source freshness will block its stale code. Passing deterministic tests is not a guarantee that a future stochastic provider output will comply. No live call is authorized or performed by this report.
