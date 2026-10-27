# Professional shared renderer and package contract v1

The v1 ProductSpec remains frozen. Its entity `display_field` is the declared
search key; enum, reference and boolean fields supply exact filters. Text-field
`min` and `max` are character counts. A board groups records by its declared
workflow enum (or its first enum if it has no workflow). Metrics appear on the
primary view, including boards and galleries, and are recomputed from that
view's selected records. Search/filter selection never writes to SQLite.

These capabilities are compiled by `runtime.presentation_contract`, rendered
by the trusted `render_app`, and checked by the real Chromium acceptance
journey before Teuta completion and again on the final package. An owner request
for search outside the display field or an unsupported filter must remain an
uncertainty rather than a promise in a v1 ProductSpec.

| Frozen ILIRIA failure | Earliest shared cause | Generic repair |
| --- | --- | --- |
| `dashboard`, `acceptance:10` | Metrics were gated on a `dashboard` view; filtering had no transport | Primary-view metrics from selected records |
| `state:validation_error`, `acceptance:12` | Text `max` was stored but not enforced | Runtime create/edit validation plus form bounds |
| `acceptance:13` | No rendered search control or empty result | Display-field search, combined filters and empty state |
| `overflow`, `acceptance:1`, `acceptance:18` | Long content could widen cards/columns | Wrapping, bounded cards, adaptive board and drawer |
| `acceptance:20` | Owner journey inherited the failures above | Direct browser steps for each capability |

The earlier evaluator also mapped board columns, card fields, filters and the
detail panel to unrelated workflow observations. Those criteria now require
their own browser assertions. Missing observations remain `NOT_RUN`; no
requirement is approved from a mapping alone.

A same-run continuation verifies the original package and its immutable
evidence, then copies the generated station modules byte-for-byte into a new
segment with the current trusted runtime. It reruns the original frozen gates,
HTTP/SQLite smoke, Chromium quality audit, full browser acceptance and Medaurus
before writing an APP READY receipt. The new package and runtime hashes,
ProductSpec hash, gate-contract hash and source identity are retained in
`shared_renderer_requalification.json`. A failed local repair is recorded as
`SHARED_RENDERER_OR_PACKAGE_CONTRACT_DEFECT`; it cannot promote the old package.
This is a governed local repair within the existing recovery budget, not a new
station or a provider retry.
