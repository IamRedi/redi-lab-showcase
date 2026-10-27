# Visual placement contract repair v1

## Retained failure, read only

The historical run `wb-958effb89bb7` remains under `build/workbench-builds/`. Its 25 files had tree digest `f5d8f5e0112815df4a3b42e255593e9a622d2f423c010437783ea4e2be74cfa3` before this repair. The run recorded three specification calls, USD 0.1154598 total cost, zero image calls, no visual assets, and `OWNER_ACTION_REQUIRED`. No historical file is edited by this repair.

All three retained Ora outputs used the old visual wire shape, which declared views but no slots. Their visual requirements therefore had only a free-form `placement` string:

| Attempt | Output file SHA-256 | Views / declared slots | Visual placements | First failing validation | Correction supplied to next attempt |
|---|---|---|---|---|---|
| 1 | `8200bc9843a4415d62c38341588caecfdf35e2cb24d24dcbf0c798835845ca31` | `journal` feed, no entity, no slots | `hero_art → hero`; `section_art → section` | `view references unknown entity` | Generic `ProductSpec schema and semantics`, the original output, and that error; no valid placement targets |
| 2 | `41515f7ddf5419fb25736137dbba30563730ba9f07be775169c344e466eb911e` | `journal` feed, no entity, no slots | `hero_art → Journal hero slot`; `section_art → Journal secondary section slot` | `view references unknown entity` | The same generic entity error and output; no valid placement targets |
| 3 | `9c60e2928e648ab70fb793878f5747275fe7d15ad68ad428e5fd41bde426f426` | `journal` feed, entity `journal`, no slots | Same prose placements as attempt 2 | `Visual placement must name a declared view and slot` | Recovery exhausted; no fourth call |

The third output corrected the entity reference, exposing the placement failure that was already present in every output. The old prompt named a limited set of slot words but did not require any slot declaration. The first two recovery prompts repeated an entity error without a finite list of valid targets. The old validator accepted a `view:slot` string if the view appeared in navigation and the slot matched a hard-coded word; it could not prove the slot belonged to the view because slots were absent from ProductSpec.

## Canonical contract

Visual ProductSpec v2 views now declare bounded `slots`, each with stable `id`, semantic `purpose`, `region` (`header` or `content`), and `accepts_visual`. A visual requirement carries `view_id` and `slot_id`; the provider wire schema has no prose `placement` field. Ora creates views and slots first and copies an exact pair into each visual requirement. Validation rejects unknown views, missing slots, cross-view slots, nonvisual slots, duplicate targets, and prose-only placement before the DAG is frozen. A composite `view_id:slot_id` is derived for existing renderer keys; it is never interpreted as an independent source of truth.

`INVALID_VISUAL_PLACEMENT` evidence includes the invalid pair, a typed reason, and at most 48 valid targets with their purposes and regions. A corrected Ora prompt receives that exact bounded list. If the next attempt repeats the same structural reason, recovery stops before another provider call. This does not choose a target for Ora when owner intent is ambiguous.

VisualTask, VisualAssetManifest, Teuta handoff, packaged handoff, renderer lookup, and final visual integration evidence carry or verify the same `view_id` and `slot_id`. Rendering iterates declared header and content slots. ProductSpec v1 without visual requirements retains its existing wire and runtime behavior. The OpenAI image worker and `gpt-image-2` request configuration are unchanged.

## Historical continuation boundary

The retained run has no frozen ProductSpec, so re-entering specification under a new contract is the correct future stage. Existing owner-decision code deliberately exposes no decision choice for its `RECOVERY_EXHAUSTED` specification blocker. It supports `RESOLVE_SPEC_DECISION` only for a recorded `SPEC_DECISION_REQUIRED` uncertainty, and its generic specification resume does not reattach the authorized ZANA visual worker/reviewer or the smoke-specific two-asset and call bounds. A safe same-logical-run resume therefore needs a typed pre-freeze structural-repair decision and a bounded visual worker handoff in the continuation segment. Reusing the old three calls and USD 0.1154598 is supported by the provider prior-state ledger, but bypassing the missing decision/worker contracts would be unsafe. No decision was submitted and no continuation segment was created for `wb-958effb89bb7`.
